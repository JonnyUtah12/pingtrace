"""Tests for github_sync — the GitHub Classic REST client and the syncer.

No network: every HTTP call is intercepted by FakeGitHubHTTP, which replaces
urllib.request.urlopen for the duration of the test (github_sync builds its
requests with urllib.request.Request, so the fake sees the real Request
objects — method, URL, JSON body and headers are all assertable).
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

import config
import database
import github_sync


# --------------------------------------------------------------- fake HTTP
class FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(url: str, code: int, message: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, message, {},
                                  io.BytesIO(json.dumps({"message": message}).encode()))


class FakeGitHubHTTP:
    """Routes (method, path-substring) -> payload dict, callable(req)->payload,
    or an exception to raise."""

    def __init__(self):
        self.routes: list[tuple[str, str, object]] = []
        self.requests: list[dict] = []

    def route(self, method: str, path_substr: str, handler):
        self.routes.append((method, path_substr, handler))

    def __call__(self, req, timeout=None):
        method = req.get_method()
        url = req.full_url
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        self.requests.append({
            "method": method, "url": url, "body": body,
            "headers": {k.lower(): v for k, v in req.header_items()},
        })
        for m, p, h in self.routes:
            if m == method and p in url:
                if isinstance(h, Exception):
                    raise h
                if callable(h):
                    h = h(req)
                return FakeResponse(h)
        raise AssertionError(f"unexpected request: {method} {url}")


@pytest.fixture
def http(monkeypatch):
    fake = FakeGitHubHTTP()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


@pytest.fixture
def client(http):
    return github_sync.GitHubClient("ghp_test", "owner/repo")


# ------------------------------------------------------------ credentials
def test_load_credentials_env_precedence(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "app_root", lambda: str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_from_env")
    monkeypatch.setenv("PINGTRACE_GITHUB_REPO", "someone/else")
    monkeypatch.setenv("PINGTRACE_GITHUB_BRANCH", "trunk")
    creds = github_sync.load_credentials()
    assert creds["token"] == "ghp_from_env"
    assert creds["repo"] == "someone/else"
    assert creds["branch"] == "trunk"
    assert creds["source"] == "env"


def test_load_credentials_from_github_json(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("PINGTRACE_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("PINGTRACE_GITHUB_REPO", raising=False)
    monkeypatch.delenv("PINGTRACE_GITHUB_BRANCH", raising=False)
    monkeypatch.setattr(config, "app_root", lambda: str(tmp_path))
    (tmp_path / "github.json").write_text(json.dumps({
        "token": "ghp_file", "repo": "me/pingtrace", "branch": "main",
        "alert_loss_pct": 40, "sync_minutes": 1,  # below the floor -> clamped
    }))
    creds = github_sync.load_credentials()
    assert creds["token"] == "ghp_file"
    assert creds["repo"] == "me/pingtrace"
    assert creds["alert_loss_pct"] == 40.0
    assert creds["sync_minutes"] == 5  # clamped to the 5-minute floor
    assert creds["source"] == str(tmp_path / "github.json")


def test_load_credentials_bad_json_falls_back_to_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("PINGTRACE_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(config, "app_root", lambda: str(tmp_path))
    (tmp_path / "github.json").write_text("{not json")
    creds = github_sync.load_credentials()
    assert creds["token"] == ""
    assert creds["repo"] == ""
    assert creds["branch"] == github_sync.DEFAULT_BRANCH


# ------------------------------------------------------------- client HTTP
def test_request_sends_classic_api_headers(client, http):
    http.route("GET", "/user", {"login": "octocat"})
    assert client.whoami() == {"login": "octocat"}
    sent = http.requests[-1]
    assert sent["headers"]["authorization"] == "Bearer ghp_test"
    assert sent["headers"]["accept"] == "application/vnd.github+json"
    assert sent["headers"]["x-github-api-version"] == "2022-11-28"
    assert config.APP_NAME in sent["headers"]["user-agent"]


def test_post_payload_is_json_encoded(client, http):
    http.route("POST", "/issues", {"html_url": "https/x/1", "number": 1})
    client.create_issue("t", "b", ["pingtrace-alert"])
    sent = http.requests[-1]
    assert sent["url"].endswith("/repos/owner/repo/issues")
    assert sent["body"] == {"title": "t", "body": "b", "labels": ["pingtrace-alert"]}


def test_404_allow_404_returns_none(client, http):
    http.route("GET", "/repos/owner/repo", http_error("u", 404, "Not Found"))
    assert client.get_repo() is None


def test_404_without_allow_raises_github_error(client, http):
    http.route("GET", "/user", http_error("u", 404, "Not Found"))
    with pytest.raises(github_sync.GitHubError) as exc:
        client.whoami()
    assert exc.value.status == 404
    assert "Not Found" in exc.value.message


def test_http_error_message_is_extracted_from_body(client, http):
    http.route("POST", "/issues", http_error("u", 422, "Validation Failed"))
    with pytest.raises(github_sync.GitHubError) as exc:
        client.create_issue("t", "b")
    assert exc.value.status == 422
    assert exc.value.message == "Validation Failed"


def test_network_error_maps_to_status_zero(client, http):
    http.route("GET", "/user", urllib.error.URLError("no route to host"))
    with pytest.raises(github_sync.GitHubError) as exc:
        client.whoami()
    assert exc.value.status == 0
    assert "network error" in exc.value.message


def test_open_alert_issues_filters_by_label(client, http):
    http.route("GET", "/issues", [])
    client.open_alert_issues()
    assert "state=open" in http.requests[-1]["url"]
    assert "labels=pingtrace-alert" in http.requests[-1]["url"]


def test_create_release_payload(client, http):
    http.route("POST", "/releases", {"html_url": "https/x/release"})
    client.create_release("v9.9.9", "Nine")
    body = http.requests[-1]["body"]
    assert body["tag_name"] == "v9.9.9" and body["name"] == "Nine"
    assert body["target_commitish"] == github_sync.DEFAULT_BRANCH


# ------------------------------------------------------------- git commits
def test_commit_files_updates_existing_branch(client, http):
    http.route("GET", "/git/ref/heads/main", {"object": {"sha": "PARENT"}})

    def blob_response(req):
        # blobs carry base64 content, not paths: b64(b"1")="MQ==", b64(b"2")="Mg=="
        return {"sha": "B1" if "MQ==" in req.data.decode() else "B2"}

    http.route("POST", "/git/blobs", blob_response)
    http.route("GET", "/git/commits/PARENT", {"tree": {"sha": "BASE"}})
    http.route("POST", "/git/trees", {"sha": "TREE"})
    http.route("POST", "/git/commits", {"sha": "NEW"})
    http.route("PATCH", "/git/refs/heads/main", {})

    commit = client.commit_files({"a.txt": b"1", "b.txt": b"2"}, "msg", "main")
    assert commit == {"sha": "NEW"}
    patch = next(r for r in http.requests if r["method"] == "PATCH")
    assert patch["body"] == {"sha": "NEW"}
    tree = next(r for r in http.requests if r["method"] == "POST" and "/git/trees" in r["url"])
    assert tree["body"]["base_tree"] == "BASE"
    assert [(e["path"], e["sha"]) for e in tree["body"]["tree"]] == [("a.txt", "B1"), ("b.txt", "B2")]


def test_commit_files_first_commit_creates_ref(client, http):
    http.route("GET", "/git/ref/heads/main", http_error("u", 404, "Not Found"))
    http.route("POST", "/git/blobs", {"sha": "B1"})
    http.route("POST", "/git/trees", {"sha": "TREE"})
    http.route("POST", "/git/commits", {"sha": "NEW"})
    http.route("POST", "/git/refs", {})

    commit = client.commit_files({"a.txt": b"1"}, "first", "main")
    assert commit == {"sha": "NEW"}
    created = next(r for r in http.requests if r["method"] == "POST"
                   and r["url"].endswith("/git/refs"))
    assert created["body"] == {"ref": "refs/heads/main", "sha": "NEW"}
    assert not any(r["method"] == "PATCH" for r in http.requests)


# ---------------------------------------------------------------- syncer
@pytest.fixture
def store(tmp_path):
    s = database.Store(str(tmp_path / "metrics.db"))
    yield s
    s.close()


@pytest.fixture
def settings():
    return config.Settings()


def make_syncer(store, settings, http, creds=None):
    creds = creds or {"token": "ghp_test", "repo": "owner/repo", "branch": "main",
                      "alert_loss_pct": 25.0, "sync_minutes": 30, "source": "test"}
    return github_sync.GitHubSyncer(store, settings, creds)


def seed_lossy(store, host, buckets=12, fail_every=2):
    """One probe per minute for `buckets` minutes; every `fail_every`-th fails."""
    t = store.add_target(host, 443, "tcp", 5.0)
    now = database.now_ms()
    for i in range(buckets):
        ts = now - i * 60_000 - 5_000
        ok = fail_every <= 0 or (i % fail_every) != 0
        store.record_probe(t["id"], ts, ok, 12.0 if ok else None,
                           None if ok else "timeout")
    return t


def test_sustained_loss_flags_only_over_threshold(store, settings):
    seed_lossy(store, "bad.example", buckets=12, fail_every=2)      # ~50% loss
    seed_lossy(store, "good.example", buckets=12, fail_every=0)    # no failures
    seed_lossy(store, "sparse.example", buckets=5)                  # < MIN_PROBES
    syncer = make_syncer(store, settings, None)
    offenders = {o["target"]["host"]: o for o in syncer.sustained_loss()}
    assert "bad.example" in offenders
    assert offenders["bad.example"]["worst_loss_pct"] >= 25.0
    assert "good.example" not in offenders
    assert "sparse.example" not in offenders  # too few buckets to alert on


def test_sustained_loss_disabled_when_threshold_zero(store, settings):
    seed_lossy(store, "bad.example")
    syncer = make_syncer(store, settings, None,
                         creds={"token": "t", "repo": "r/r", "branch": "main",
                                "alert_loss_pct": 0, "sync_minutes": 30})
    assert syncer.sustained_loss() == []


def test_check_alerts_creates_issue_with_label(store, settings, http):
    seed_lossy(store, "bad.example")
    http.route("GET", "/issues", [])
    http.route("POST", "/issues", {"html_url": "https/github/issues/7", "number": 7})
    syncer = make_syncer(store, settings, http)
    result = syncer.check_alerts()
    assert result["ok"] and len(result["created"]) == 1
    created = result["created"][0]
    assert created["host"] == "bad.example" and created["number"] == 7
    post = next(r for r in http.requests if r["method"] == "POST" and "/issues" in r["url"])
    assert post["body"]["title"] == "[pingtrace] sustained packet loss on bad.example"
    assert post["body"]["labels"] == [github_sync.ALERT_LABEL]
    assert "worst bucket loss" in post["body"]["body"]


def test_check_alerts_skips_when_issue_already_open(store, settings, http):
    seed_lossy(store, "bad.example")
    title = "[pingtrace] sustained packet loss on bad.example"
    http.route("GET", "/issues", [{"title": title}])
    syncer = make_syncer(store, settings, http)
    result = syncer.check_alerts()
    assert result["ok"] and result["created"] == []
    assert result["skipped"][0]["host"] == "bad.example"
    assert not any(r["method"] == "POST" for r in http.requests)


def test_check_alerts_reports_api_failure_without_raising(store, settings, http):
    seed_lossy(store, "bad.example")
    http.route("GET", "/issues", http_error("u", 401, "Bad credentials"))
    syncer = make_syncer(store, settings, http)
    result = syncer.check_alerts()
    assert result["ok"] is False
    assert "Bad credentials" in result["detail"]
    assert "Bad credentials" in syncer.last_error


def test_build_snapshot_produces_three_state_files(store, settings):
    seed_lossy(store, "a.example", buckets=3)
    syncer = make_syncer(store, settings, None)
    files = syncer.build_snapshot()
    assert sorted(files) == ["state/last-hour.json", "state/performance-summary.md",
                             "state/targets.json"]
    targets = json.loads(files["state/targets.json"])
    assert targets["targets"][0]["host"] == "a.example"
    stats = json.loads(files["state/last-hour.json"])
    assert stats["window"] == "1h" and len(stats["targets"]) == 1
    summary = files["state/performance-summary.md"].decode()
    assert "| target | probes |" in summary and "a.example" in summary


def test_sync_now_records_commit_and_handles_errors(store, settings, http):
    seed_lossy(store, "a.example", buckets=2)
    http.route("GET", "/git/ref/heads/main", {"object": {"sha": "PARENT"}})
    http.route("POST", "/git/blobs", {"sha": "B"})
    http.route("GET", "/git/commits/PARENT", {"tree": {"sha": "BASE"}})
    http.route("POST", "/git/trees", {"sha": "TREE"})
    http.route("POST", "/git/commits", {"sha": "NEW", "html_url": "https/c/NEW"})
    http.route("PATCH", "/git/refs/heads/main", {})
    syncer = make_syncer(store, settings, http)
    result = syncer.sync_now()
    assert result["ok"] and result["sha"] == "NEW"
    assert syncer.last_sync["sha"] == "NEW"
    assert syncer.last_sync["files"] == ["state/last-hour.json",
                                         "state/performance-summary.md",
                                         "state/targets.json"]

    http.routes = [("GET", "/git/ref/heads/main", http_error("u", 403, "forbidden"))]
    result = syncer.sync_now()
    assert result["ok"] is False and "forbidden" in result["detail"]
    assert "forbidden" in syncer.last_error


def test_syncer_disabled_without_token(settings):
    syncer = github_sync.GitHubSyncer(None, settings,
                                      {"token": "", "repo": "", "branch": "main"})
    assert syncer.enabled is False and syncer.client is None
    assert syncer.sync_now()["ok"] is False
    assert syncer.check_alerts()["ok"] is False


def test_status_exposes_config(store, settings):
    syncer = make_syncer(store, settings, None)
    st = syncer.status()
    assert st["enabled"] is True
    assert st["repo"] == "owner/repo" and st["branch"] == "main"
    assert st["alert_loss_pct"] == 25.0 and st["sync_minutes"] == 30
    assert st["config_source"] == "test"
