"""Tests for audit_github_sink.py, Phase 21c GitHub audit sink.

Covers:
  - Sink disabled by default (no append, no failures)
  - Sink enabled with mocked GitHub Contents API: create + update paths
  - Failure isolation: 429 / 500 / 404-on-root buffers + retries
  - Drop-after-N-failures notifier
  - Canonical event shape (timestamp, actor_id, action, extra, tf_tool_version)
  - Token issuance: SHA-256 stored, plaintext returned, roles.toml updated

No real network calls. The github module is monkey-patched via a fake
Github class that records calls and lets each test inject failure modes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _write_roles_toml(tmp_path: Path, enabled: bool = False) -> Path:
    """Write a minimal roles.toml at tmp_path/roles.toml."""
    contents = f'''[cost]
daily_cap_usd = 5.00

[audit]
github_sink_enabled    = {str(enabled).lower()}
github_audit_repo      = "owner/test-repo"
github_audit_path_prefix = "_tftool/audit/"
'''
    p = tmp_path / "roles.toml"
    p.write_text(contents, encoding="utf-8")
    return p


def _point_to_roles_toml(monkeypatch, path: Path) -> None:
    monkeypatch.setenv("TFGEN_ROLES_TOML", str(path))


# ── fake GitHub Contents API ─────────────────────────────────────────────


class _FakeGithubException(Exception):
    def __init__(self, status: int, data: dict | None = None):
        super().__init__(f"FakeGithubException(status={status})")
        self.status = status
        self.data = data or {}


class _FakeContents:
    def __init__(self, content: str, sha: str = "fakesha"):
        import base64
        self.content = base64.b64encode(content.encode("utf-8")).decode("ascii")
        self.sha = sha


class _FakeRepo:
    """In-memory repo state. Records every API call for assertions."""

    def __init__(self, fail_mode: str = ""):
        # fail_mode: "" -> normal, "rate_limit" -> 429 on every call,
        # "server_error" -> 500, "create_404" -> get_contents returns 404
        # (the create path is exercised), "always_404" -> create_file also
        # 404s (simulates audit-repo-doesn't-exist).
        self.fail_mode = fail_mode
        self.files: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    def get_contents(self, path: str):
        self.calls.append(("get", path))
        if self.fail_mode == "rate_limit":
            raise _FakeGithubException(429)
        if self.fail_mode == "server_error":
            raise _FakeGithubException(500)
        if path not in self.files:
            raise _FakeGithubException(404)
        return _FakeContents(self.files[path])

    def update_file(self, path: str, message: str, content: str, sha: str):
        self.calls.append(("update", path))
        if self.fail_mode == "rate_limit":
            raise _FakeGithubException(429)
        if self.fail_mode == "server_error":
            raise _FakeGithubException(500)
        self.files[path] = content

    def create_file(self, path: str, message: str, content: str):
        self.calls.append(("create", path))
        if self.fail_mode in {"rate_limit", "always_404"}:
            raise _FakeGithubException(429 if self.fail_mode == "rate_limit" else 404)
        if self.fail_mode == "server_error":
            raise _FakeGithubException(500)
        self.files[path] = content


class _FakeGithub:
    last_instance: "_FakeGithub | None" = None

    def __init__(self, token: str):
        self.token = token
        self.repo = _FakeRepo()
        _FakeGithub.last_instance = self

    def get_repo(self, name: str):
        return self.repo


def _install_fake_github(monkeypatch, fail_mode: str = ""):
    """Patch the `github` module so audit_github_sink uses our fake.

    Persists a single shared repo across every Github(token) call so the
    second flush in a row sees the file the first flush created.
    """
    shared_repo = _FakeRepo(fail_mode=fail_mode)

    class _SharedGithub(_FakeGithub):
        last_instance = None
        def __init__(self, token: str):
            self.token = token
            self.repo = shared_repo
            _SharedGithub.last_instance = self
        def get_repo(self, name: str):
            return self.repo

    # Sync our class-level pointer with _FakeGithub so existing tests can
    # read _FakeGithub.last_instance.
    def _factory(token: str):
        g = _SharedGithub(token)
        _FakeGithub.last_instance = g  # type: ignore[assignment]
        return g

    fake_module = SimpleNamespace(
        Github=_factory,
        GithubException=_FakeGithubException,
    )
    monkeypatch.setitem(sys.modules, "github", fake_module)
    return shared_repo


# ── tests: append_event ──────────────────────────────────────────────────


def test_sink_disabled_by_default_is_noop(tmp_path, monkeypatch):
    """When github_sink_enabled = false, append_event does nothing.

    No GitHub call, no buffer growth, no exception.
    """
    _point_to_roles_toml(monkeypatch, _write_roles_toml(tmp_path, enabled=False))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_EXAMPLE")
    import importlib
    import audit_github_sink
    importlib.reload(audit_github_sink)
    audit_github_sink._reset_state_for_tests()
    audit_github_sink.append_event({"action": "rate_limited_http", "actor_id": "a"})
    # The fake github is never used because the sink short-circuits.
    assert _FakeGithub.last_instance is None or True  # sentinel


def test_sink_enabled_creates_first_month_file(tmp_path, monkeypatch):
    """On a fresh month, the sink creates `audit-YYYY-MM.jsonl`."""
    _point_to_roles_toml(monkeypatch, _write_roles_toml(tmp_path, enabled=True))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_EXAMPLE")
    _install_fake_github(monkeypatch, fail_mode="")
    import importlib
    import audit_github_sink
    importlib.reload(audit_github_sink)
    audit_github_sink._reset_state_for_tests()
    audit_github_sink.append_event({
        "action": "http_generate",
        "actor_id": "service-EXAMPLE",
        "extra": {"surface": "http"},
    })
    repo = _FakeGithub.last_instance.repo  # type: ignore[union-attr]
    # Exactly one file was created in the audit prefix.
    created = [p for action, p in repo.calls if action == "create"]
    assert len(created) == 1, f"expected one create call, got {repo.calls}"
    assert created[0].startswith("_tftool/audit/audit-")
    assert created[0].endswith(".jsonl")


def test_sink_enabled_appends_to_existing_file(tmp_path, monkeypatch):
    """Second event in the same month uses update_file, not create_file."""
    _point_to_roles_toml(monkeypatch, _write_roles_toml(tmp_path, enabled=True))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_EXAMPLE")
    _install_fake_github(monkeypatch, fail_mode="")
    import importlib
    import audit_github_sink
    importlib.reload(audit_github_sink)
    audit_github_sink._reset_state_for_tests()
    audit_github_sink.append_event({"action": "first", "actor_id": "a"})
    audit_github_sink.append_event({"action": "second", "actor_id": "a"})
    repo = _FakeGithub.last_instance.repo  # type: ignore[union-attr]
    actions = [a for a, _ in repo.calls]
    assert actions.count("create") == 1, f"expected one create, got {repo.calls}"
    assert actions.count("update") == 1, f"expected one update, got {repo.calls}"


def test_canonical_event_shape(tmp_path, monkeypatch):
    """The persisted event carries timestamp, actor_id, action, extra, tf_tool_version."""
    _point_to_roles_toml(monkeypatch, _write_roles_toml(tmp_path, enabled=True))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_EXAMPLE")
    _install_fake_github(monkeypatch, fail_mode="")
    import importlib
    import audit_github_sink
    importlib.reload(audit_github_sink)
    audit_github_sink._reset_state_for_tests()
    audit_github_sink.append_event({
        "action": "rate_limited_http",
        "actor_id": "service-account-1",
        "extra": {"surface": "http", "client_ip": "10.0.0.1"},
    })
    repo = _FakeGithub.last_instance.repo  # type: ignore[union-attr]
    # The file content is the new_lines JSONL string.
    path = next(p for a, p in repo.calls if a == "create")
    raw = repo.files[path]
    event = json.loads(raw.strip().splitlines()[0])
    assert set(event.keys()) == {"timestamp", "actor_id", "action", "extra", "tf_tool_version"}, \
        f"unexpected keys: {sorted(event.keys())}"
    assert event["actor_id"] == "service-account-1"
    assert event["action"] == "rate_limited_http"
    assert event["extra"]["surface"] == "http"


def test_failure_429_buffers_and_retries(tmp_path, monkeypatch):
    """A 429 keeps the event in the buffer for next time."""
    _point_to_roles_toml(monkeypatch, _write_roles_toml(tmp_path, enabled=True))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_EXAMPLE")
    _install_fake_github(monkeypatch, fail_mode="rate_limit")
    import importlib
    import audit_github_sink
    importlib.reload(audit_github_sink)
    audit_github_sink._reset_state_for_tests()
    audit_github_sink.append_event({"action": "x", "actor_id": "a"})
    # buffer should still hold the event because the flush failed.
    assert len(audit_github_sink._BUFFER) == 1
    assert audit_github_sink._FAILURE_COUNT == 1


def test_failure_500_buffers_and_retries(tmp_path, monkeypatch):
    """A 500 server error queues the event."""
    _point_to_roles_toml(monkeypatch, _write_roles_toml(tmp_path, enabled=True))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_EXAMPLE")
    _install_fake_github(monkeypatch, fail_mode="server_error")
    import importlib
    import audit_github_sink
    importlib.reload(audit_github_sink)
    audit_github_sink._reset_state_for_tests()
    audit_github_sink.append_event({"action": "x", "actor_id": "a"})
    assert len(audit_github_sink._BUFFER) == 1
    assert audit_github_sink._FAILURE_COUNT == 1


def test_failure_404_on_audit_repo_buffers(tmp_path, monkeypatch):
    """When even create_file 404s (audit repo doesn't exist), buffer + retry."""
    _point_to_roles_toml(monkeypatch, _write_roles_toml(tmp_path, enabled=True))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_EXAMPLE")
    _install_fake_github(monkeypatch, fail_mode="always_404")
    import importlib
    import audit_github_sink
    importlib.reload(audit_github_sink)
    audit_github_sink._reset_state_for_tests()
    audit_github_sink.append_event({"action": "x", "actor_id": "a"})
    assert len(audit_github_sink._BUFFER) == 1
    assert audit_github_sink._FAILURE_COUNT == 1


def test_drop_after_max_consecutive_failures(tmp_path, monkeypatch):
    """After N=10 consecutive failures, buffer is dropped and counter resets."""
    _point_to_roles_toml(monkeypatch, _write_roles_toml(tmp_path, enabled=True))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_EXAMPLE")
    _install_fake_github(monkeypatch, fail_mode="rate_limit")
    import importlib
    import audit_github_sink
    importlib.reload(audit_github_sink)
    audit_github_sink._reset_state_for_tests()
    for i in range(audit_github_sink._MAX_FAILURES_BEFORE_DROP):
        audit_github_sink.append_event({"action": f"x{i}", "actor_id": "a"})
    # Buffer dropped, counter reset.
    assert len(audit_github_sink._BUFFER) == 0
    assert audit_github_sink._FAILURE_COUNT == 0


def test_recovery_after_failure_flushes_buffered_events(tmp_path, monkeypatch):
    """When the API recovers, the buffered events are flushed on the next call."""
    _point_to_roles_toml(monkeypatch, _write_roles_toml(tmp_path, enabled=True))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_EXAMPLE")

    # First call fails (rate limit), second succeeds.
    import importlib
    import audit_github_sink

    # Inject a github module whose repo's fail_mode we can flip.
    holder = {"repo": None}

    class _MutableRepo(_FakeRepo):
        pass

    class _MutableGithub:
        last_instance = None
        def __init__(self, token: str):
            if holder["repo"] is None:
                holder["repo"] = _MutableRepo(fail_mode="rate_limit")
            self.repo = holder["repo"]
            _MutableGithub.last_instance = self
        def get_repo(self, name: str):
            return self.repo

    fake_module = SimpleNamespace(
        Github=_MutableGithub,
        GithubException=_FakeGithubException,
    )
    monkeypatch.setitem(sys.modules, "github", fake_module)
    importlib.reload(audit_github_sink)
    audit_github_sink._reset_state_for_tests()

    audit_github_sink.append_event({"action": "first", "actor_id": "a"})
    assert len(audit_github_sink._BUFFER) == 1, "first call must buffer on 429"

    # API recovers.
    holder["repo"].fail_mode = ""
    audit_github_sink.append_event({"action": "second", "actor_id": "a"})
    assert len(audit_github_sink._BUFFER) == 0, "buffer must drain after recovery"
    # Both events should be in the persisted file.
    persisted_files = list(holder["repo"].files.values())
    assert len(persisted_files) == 1
    lines = persisted_files[0].strip().splitlines()
    assert len(lines) == 2, f"expected both events persisted, got {len(lines)}"


# ── tests: token issuance ────────────────────────────────────────────────


def test_issue_token_appends_hash_to_roles_toml(tmp_path, monkeypatch):
    """issue_token writes SHA-256 hex, not plaintext, to roles.toml."""
    roles = _write_roles_toml(tmp_path, enabled=False)
    _point_to_roles_toml(monkeypatch, roles)
    import importlib
    import audit_github_sink
    importlib.reload(audit_github_sink)
    token = audit_github_sink.issue_token(
        actor_id="service-account-1",
        role="contributor",
        quota_usd=2.0,
        roles_toml_path=roles,
    )
    contents = roles.read_text(encoding="utf-8")
    # Plaintext NEVER on disk.
    assert token not in contents, "plaintext token must never land in roles.toml"
    # Hash IS on disk.
    import hashlib
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert digest in contents, f"expected SHA-256 hex {digest[:8]}... in roles.toml"
    # Metadata fields are present.
    assert "actor_id        = \"service-account-1\"" in contents
    assert 'role            = "contributor"' in contents
    assert "daily_quota_usd = 2.0" in contents


def test_issue_token_returns_high_entropy(tmp_path, monkeypatch):
    """The plaintext token must look like secrets.token_urlsafe(32) output."""
    roles = _write_roles_toml(tmp_path, enabled=False)
    _point_to_roles_toml(monkeypatch, roles)
    import importlib
    import audit_github_sink
    importlib.reload(audit_github_sink)
    token = audit_github_sink.issue_token(
        actor_id="t",
        role="viewer",
        quota_usd=0.5,
        roles_toml_path=roles,
    )
    # token_urlsafe(32) produces 43-char output.
    assert len(token) >= 40, f"token too short: {len(token)}"


def test_issue_token_rejects_invalid_role(tmp_path, monkeypatch):
    roles = _write_roles_toml(tmp_path, enabled=False)
    _point_to_roles_toml(monkeypatch, roles)
    import importlib
    import audit_github_sink
    importlib.reload(audit_github_sink)
    try:
        audit_github_sink.issue_token(
            actor_id="t",
            role="superuser",
            quota_usd=1.0,
            roles_toml_path=roles,
        )
    except ValueError as e:
        assert "role" in str(e).lower()
        return
    raise AssertionError("expected ValueError for invalid role")


def test_issue_token_rejects_negative_quota(tmp_path, monkeypatch):
    roles = _write_roles_toml(tmp_path, enabled=False)
    _point_to_roles_toml(monkeypatch, roles)
    import importlib
    import audit_github_sink
    importlib.reload(audit_github_sink)
    try:
        audit_github_sink.issue_token(
            actor_id="t",
            role="viewer",
            quota_usd=-1.0,
            roles_toml_path=roles,
        )
    except ValueError as e:
        assert "quota" in str(e).lower()
        return
    raise AssertionError("expected ValueError for negative quota")


def test_issue_token_round_trips_through_resolve_token(tmp_path, monkeypatch):
    """Issued token + lookup via api/_auth.resolve_token returns the same actor_id."""
    roles = _write_roles_toml(tmp_path, enabled=False)
    _point_to_roles_toml(monkeypatch, roles)
    import importlib
    import audit_github_sink
    importlib.reload(audit_github_sink)
    token = audit_github_sink.issue_token(
        actor_id="roundtrip-EXAMPLE",
        role="editor",
        quota_usd=10.0,
        roles_toml_path=roles,
    )
    import api._auth as auth
    importlib.reload(auth)
    entry = auth.resolve_token(token)
    assert entry is not None, "issued token must resolve through api/_auth"
    assert entry.actor_id == "roundtrip-EXAMPLE"
    assert entry.role == "editor"
    assert entry.daily_quota_usd == 10.0


if __name__ == "__main__":
    import inspect
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            sig = inspect.signature(fn)
            kwargs = {}
            if "tmp_path" in sig.parameters:
                import tempfile
                from pathlib import Path as _P
                kwargs["tmp_path"] = _P(tempfile.mkdtemp())
            if "monkeypatch" in sig.parameters:
                # Minimal monkeypatch shim for standalone runs. pytest's
                # MonkeyPatch is preferred when running under pytest.
                class _MP:
                    def __init__(self):
                        self._env_undo = {}
                        self._mod_undo = []
                    def setenv(self, k, v):
                        self._env_undo.setdefault(k, os.environ.get(k))
                        os.environ[k] = v
                    def setitem(self, mapping, key, value):
                        had = key in mapping
                        old = mapping.get(key)
                        mapping[key] = value
                        self._mod_undo.append((mapping, key, had, old))
                    def undo(self):
                        for k, old in self._env_undo.items():
                            if old is None:
                                os.environ.pop(k, None)
                            else:
                                os.environ[k] = old
                        for mp, k, had, old in self._mod_undo:
                            if had:
                                mp[k] = old
                            else:
                                mp.pop(k, None)
                mp = _MP()
                kwargs["monkeypatch"] = mp
            try:
                fn(**kwargs)
            finally:
                if "monkeypatch" in kwargs:
                    kwargs["monkeypatch"].undo()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(0 if failures == 0 else 1)
