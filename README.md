# PingTrace

Portable, no-admin multi-target latency monitor for Windows. One folder, one
exe, one localhost port: it probes a list of hosts (TCP/UDP connect probes
and real ICMP echo - never a raw socket, so no elevation), stores history in
a local SQLite file, and serves a dashboard at `http://127.0.0.1:8765/`.
Design contract: `ARCHITECTURE.md`.

## Run it (Windows)

1. Download **`pingtrace-windows-x64-portable.zip`** from the
   [latest release](https://github.com/JonnyUtah12/pingtrace/releases/latest)
   and extract it. Copy the whole `pingtrace` folder anywhere - Documents, a
   USB stick, another PC. No installer, no registry, no admin token, **no
   Python** - `pingtrace.exe` carries its own runtime inside the folder.
2. Double-click `run.bat` (or run it from `cmd`). It starts the server
   minimized, waits for `/api/health`, then opens your default browser.
3. `stop.bat` stops it. Re-running `run.bat` while it is already up just
   re-opens the browser.

The zip is built automatically on a real Windows host by the
`windows-portable` GitHub Actions workflow (it also smoke-tests `run.bat`,
the frozen exe, and `stop.bat` on every push). The git checkout itself
contains **no** `.exe` - do not copy the source folder and expect `run.bat`
to work; use the release zip, or build it yourself as below.

## Why Windows Defender / SmartScreen flags it (and what we do about it)

`pingtrace.exe` is a PyInstaller build of this open source, produced fresh
on every release - it is **unsigned**, so SmartScreen has no publisher and
no download reputation for it, and Defender's heuristics are suspicious of
PyInstaller binaries in general. That is why the browser warns about the
zip and SmartScreen may say "Windows protected your PC".

What the app already does about it:

- The build is **onedir** (a normal folder of visible files), not a
  one-file exe that unpacks itself into `%TEMP%` - that pattern is what
  most often trips Defender's behaviour heuristics.
- `run.bat` **removes the download mark** (Mark-of-the-Web) from the
  folder on first run, so SmartScreen does not block the exe when you
  launch it. The app is built from this repo on GitHub Actions; if you
  downloaded it from the releases page, that is your source of trust.
- If Defender's real-time scan ever quarantines the exe anyway (it
  occasionally false-positives on PyInstaller), re-allow it from
  *Protection history* - or add the `pingtrace` folder to exclusions.

The real cure is a **code-signing certificate**. The workflow already
supports it: set up [Azure Trusted Signing](https://learn.microsoft.com/azure/trusted-signing/)
(~$10/month, needs org verification once), then create the repo variables
`TRUSTED_SIGNING_ENDPOINT`, `TRUSTED_SIGNING_ACCOUNT_NAME`,
`TRUSTED_SIGNING_CERT_PROFILE` and the secrets `AZURE_TENANT_ID`,
`AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` (an app registration with
permission on the signing account).
Every release build then signs `pingtrace.exe` automatically; SmartScreen
stops blocking it and reputation builds from the first download. Until
then, "Keep" / "Run anyway" on the prompt is safe.

## Folder layout

The release zip extracts to one self-contained folder (flat onedir - the
runtime files sit next to the exe, there is no installer and nothing
outside the folder):

```
pingtrace/
  run.bat, stop.bat          start / stop (cmd.exe, no admin)
  pingtrace.exe              the app with its own Python runtime inside
  static/                    dashboard html/js/css, served as-is
  targets.json               your target list (editable, see below)
  github.json                optional GitHub PAT - never committed to git
  metrics.db (+ -wal, -shm)  history, created on first run
  .pingtrace.pid             written while running, read by stop.bat
```

The source tree is the same app as plain Python: `main.py` (entry point),
`config.py`, `engine.py` (probes), `database.py` (SQLite + tiering),
`api.py` (FastAPI routes), `github_sync.py` (GitHub Classic client),
`static/`, `tests/`, `packaging/` (PyInstaller spec + build scripts), and
the `windows-portable` workflow under `.github/workflows/`.

## Why no administrator privileges (and how to read results)

Windows only lets elevated processes open a raw ICMP socket - that is why
most ping tools demand "Run as administrator". PingTrace never opens one:

- `tcp`/`udp` targets use a plain `connect()` to the probe port (default
  443), an unprivileged operation. The latency shown is the time to that
  handshake, not an ICMP round-trip.
- `icmp` targets use the same OS call `ping.exe` makes (`IcmpSendEcho`
  via `iphlpapi.dll` on Windows; an unprivileged DGRAM ICMP socket on
  Linux) - a real echo, still no elevation.
- Route traces use UDP probes with increasing TTLs, or the built-in
  `tracert` on Windows.

What that means when reading the dashboard: a TCP/UDP probe answers
"can I reach this host **at this port**", not "does it answer ping". A
host with no open probe port - or a firewall that drops the SYNs - shows
as **down** even though the host is alive. That is the price of
unprivileged probing, and the reason `proto: "icmp"` exists: use it for
hosts where no port is open. Treat "down" as "no answer at the probe
port", not "the machine is off".

## Editing targets

`targets.json` next to the exe is the target list. The shipped sample monitors
your LAN gateway (`192.168.1.1`), `8.8.8.8`, `1.1.1.1` and `github.com` -
edit it to your own hosts **while the server is stopped** (the dashboard's
add/remove UI writes the same file while running, so don't hand-edit during a
run). Each entry:

```json
{ "host": "192.168.1.1", "port": 443, "proto": "tcp",
  "interval_s": 5.0, "label": "LAN gateway", "enabled": true }
```

`proto` is `tcp`, `udp` (connect probes, port usually 443) or `icmp` (real
ICMP echo - no port needed, works even when no port is open); `interval_s` is
1-300. All three run without elevation - see ARCHITECTURE.md section 2.

## Route tracing

Every target card has a **trace route** button: PingTrace walks TTLs 1..30
with UDP probes and lists every hop that answers — address, reverse-DNS name,
and per-probe round-trip times — ending at the destination or at the last
router that replied. On Windows the hop walk runs through the built-in
`tracert` engine (ICMP echo) automatically. This needs no admin rights and
no raw sockets (see `ARCHITECTURE.md` §2). Silent hops are shown as `·` rather than hidden, and
each row is that router's own reply: it proves the outbound path *to* that
hop and the return path *from* it; return paths can differ (asymmetric
routing). The last trace per target is kept (newest 20) and readable via
`GET /api/targets/{id}/trace`.

### Continuous tracing

Each card also has a **trace loop** switch: turn it on and PingTrace keeps
re-running the route trace for that target until you turn it off — pick how
often (30s, 1m, 5m, 15m, 1h). Every target runs its own loop independently,
so you can watch the full path to several targets at once; the card's trace
panel refreshes itself as new rounds land, and the card gets a highlighted
outline while a loop is active. Loops survive restarts (the setting is
saved with the target), never overlap for the same target, and stop when
the target is disabled or deleted. Each round is stored like a manual trace,
so `GET /api/targets/{id}/traces` shows the recent history.

## GitHub sync and alert issues (optional)

Give PingTrace a GitHub **Classic** PAT (scope `repo`) and it keeps an
auditable trail of your monitoring in a repo, and opens an issue when a
target really is losing packets. Authenticate either way - environment
variables win over the file:

- `github.json` next to the exe:

  ```json
  { "token": "ghp_...", "repo": "your-user/pingtrace-state",
    "branch": "main", "alert_loss_pct": 25, "sync_minutes": 30 }
  ```

- or the environment: `PINGTRACE_GITHUB_TOKEN` (or plain `GITHUB_TOKEN`),
  optionally `PINGTRACE_GITHUB_REPO` and `PINGTRACE_GITHUB_BRANCH`.

The token is never written into the repo or into any state snapshot, and
`github.json` is git-ignored.

Every `sync_minutes` (floor 5) PingTrace commits three files under
`state/` in that repo: `targets.json` (what you monitor),
`last-hour.json` (per-target stats for the last hour) and
`performance-summary.md` (a readable table).

**How the alert issues work.** With each sync, every enabled target is
checked over the last 30 minutes of 1-minute buckets (at least 10 buckets
of data required, so a host that was just added cannot alert yet). If the
worst bucket or the mean loss reaches `alert_loss_pct` (default 25%),
PingTrace opens an issue titled `[pingtrace] sustained packet loss on
<host>`, labelled `pingtrace-alert`, with the window stats in the body.
It never opens a second issue for the same host while one is still open -
close it when you have looked, and a new incident opens a new issue.

## Where state lives

Everything stays inside the folder: `metrics.db` (+ WAL sidecars),
`targets.json`, `github.json` (your GitHub PAT - keep it out of git),
`.pingtrace.pid`. Nothing is written to the registry.

One exception, so you never need "Run as administrator": if the folder is
read-only (extracted to `C:\` root, Program Files, etc.), the mutable state
moves to `%LOCALAPPDATA%\PingTrace` instead - any history already in the app
folder is carried over automatically, and the startup window says where state
now lives. Move the folder somewhere writable to go back to fully portable
state. `github.json` is always read from the app folder.

## Building the portable folder from source

Only needed if you want to build the exe yourself - the release zip already
contains a built one. Python is required **only here**, never to run the
app:

    pip install -r requirements.txt
    packaging/build_portable.sh        # Linux/macOS
    packaging\build_portable.ps1      # Windows

Both run the PyInstaller **onedir** build (`packaging/pingtrace.spec`) and
assemble `dist/pingtrace/` with the launchers and sample state. onefile is
deliberately not used - it unpacks to a temp dir and breaks portability.

### Run from source (any OS, for development)

    pip install -r requirements.txt
    python main.py            # dashboard at http://127.0.0.1:8765/

Useful flags: `--port`, `--probe-port`, `--interval`, `--retention-hours`,
`--targets host1 host2 ...`. The test suite needs no network (the GitHub
client is mocked): `pip install pytest && pytest -q`.


## Troubleshooting

**"8765 is already in use" / the page never loads.** Another process (or
another PingTrace) holds the port. Close it, or pick another one: edit
`set "PORT=..."` in `run.bat`, or start `pingtrace.exe --port 8766`
directly and open `http://127.0.0.1:8766/`. If `run.bat` says another
instance is already running, that is by design - it just opens the
browser at the running one.

**`.pingtrace.pid`.** Written next to the exe while the server runs (in
`%LOCALAPPDATA%\PingTrace` when the app folder is read-only); `stop.bat`
reads it to find the process. A stale file left by a crash is harmless -
the app checks whether the recorded PID is actually alive before treating
it as a running instance. Delete it by hand only if `stop.bat` claims to
stop a process you cannot see.

**History and DB retention.** `metrics.db` keeps 7 days by default
(`--retention-hours` to change it). Recent hours stay at per-probe
resolution; older data inside the window is rolled up into 1-minute
buckets automatically, and anything past the window is deleted on a
timer. Stop the server and delete `metrics.db` (plus the `-wal`/`-shm`
sidecars) to start history from scratch.

## Verification status

What is verified where, honestly:

- **Linux (the build host):** the app end to end - engine, DB
  tiering/search/export, API routes, dashboard in a real browser - and
  the Linux onedir build. `pytest -q`: 93 passed, 1 skipped (the skip is
  the Windows-only `tracert` engine test).
- **Windows (CI, real `windows-latest` runner, every push and release):**
  `build_portable.ps1`, the frozen `pingtrace.exe`, the full test suite,
  the dashboard assets, the `run.bat` already-running path, and
  `stop.bat`. The release zip is produced by that same job, so what you
  download is what was smoke-tested.
- **Windows, not covered headless:** `run.bat`'s cold start via
  `start /min` needs a desktop session (a minimized console dies with
  its parent cmd on CI runners). It works on real desktops - the
  user-reported failure that led to the release zip was the missing exe,
  not `run.bat` - and CI exercises the same exe, health poll and stop
  path directly.
- **Never automated:** SmartScreen/Defender behaviour on a fresh
  download (see above) and interactive browser UI checks on Windows.
