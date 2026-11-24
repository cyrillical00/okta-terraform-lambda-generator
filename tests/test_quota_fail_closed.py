"""The daily-spend quota must fail closed on the headless surfaces.

An unreadable usage store, a store nothing can be written to, or a failed
write must never look like "nothing spent yet". No network: the GitHub
client is replaced by a small fake.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("TFGEN_API_KEY", "test-secret-do-not-ship")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-fake")

import cost
from api import _bootstrap

ACTOR = "actor-EXAMPLE"


class _GhError(Exception):
    def __init__(self, status):
        super().__init__(f"status {status}")
        self.status = status


class _FakeRepo:
    def __init__(self, stored=None, read_error=None, write_error=None):
        self.stored = stored            # dict for the usage file, or None = no file yet
        self.read_error = read_error
        self.write_error = write_error

    def get_contents(self, path):
        if self.read_error:
            raise _GhError(self.read_error)
        if self.stored is None:
            raise _GhError(404)
        body = base64.b64encode(json.dumps(self.stored).encode()).decode()
        return SimpleNamespace(content=body, sha="sha")

    def update_file(self, path, message, content, sha):
        if self.write_error:
            raise _GhError(self.write_error)
        self.stored = json.loads(content)

    def create_file(self, path, message, content):
        if self.write_error:
            raise _GhError(self.write_error)
        self.stored = json.loads(content)


def _use_github(monkeypatch, repo=None, repo_error=None):
    def _get_repo(name):
        if repo_error:
            raise _GhError(repo_error)
        return repo

    fake = SimpleNamespace(
        Github=lambda token: SimpleNamespace(get_repo=_get_repo),
        GithubException=_GhError,
    )
    monkeypatch.setitem(sys.modules, "github", fake)
    cost.configure("token-for-tests", "owner/usage-repo")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    cost.clear_session()
    cost.configure("", "")
    monkeypatch.setattr(cost, "_LOCAL_PATH", str(tmp_path / "state" / "usage.json"))
    yield
    cost.clear_session()
    cost.configure("", "")


def _ctx(quota=5.0):
    return _bootstrap.CallerContext(actor_id=ACTOR, client=None, model="m", daily_quota_usd=quota)


# ─── cost.today_usd(strict=True) ─────────────────────────────────────────


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502])
def test_github_read_error_raises_in_strict_mode(monkeypatch, status):
    _use_github(monkeypatch, _FakeRepo(read_error=status))
    with pytest.raises(cost.UsageUnavailable):
        cost.today_usd(ACTOR, strict=True)
    assert cost.today_usd(ACTOR) == 0.0, "display callers keep the lenient default"


def test_unreachable_usage_repo_raises_in_strict_mode(monkeypatch):
    # 404 on the repo itself is a broken store, not "no usage yet"
    _use_github(monkeypatch, repo_error=404)
    with pytest.raises(cost.UsageUnavailable):
        cost.today_usd(ACTOR, strict=True)


def test_missing_usage_file_is_a_real_zero(monkeypatch):
    _use_github(monkeypatch, _FakeRepo(stored=None))
    assert cost.today_usd(ACTOR, strict=True) == 0.0


def test_stored_total_is_returned(monkeypatch):
    _use_github(monkeypatch, _FakeRepo(stored={cost._today_key(): 1.25}))
    assert cost.today_usd(ACTOR, strict=True) == 1.25


def test_corrupt_usage_file_on_github_raises(monkeypatch):
    repo = _FakeRepo()
    repo.get_contents = lambda path: SimpleNamespace(content=base64.b64encode(b"{not json").decode(), sha="s")
    _use_github(monkeypatch, repo)
    with pytest.raises(cost.UsageUnavailable):
        cost.today_usd(ACTOR, strict=True)


def test_local_store_missing_file_is_zero_when_writable():
    assert cost.today_usd(ACTOR, strict=True) == 0.0


def test_local_store_corrupt_file_raises():
    os.makedirs(os.path.dirname(cost._LOCAL_PATH), exist_ok=True)
    with open(cost._LOCAL_PATH, "w", encoding="utf-8") as f:
        f.write("{not json")
    with pytest.raises(cost.UsageUnavailable):
        cost.today_usd(ACTOR, strict=True)
    assert cost.today_usd(ACTOR) == 0.0


def test_read_only_filesystem_without_github_raises():
    """The serverless case: no GitHub storage and nowhere to write locally."""
    with patch.object(cost.os, "access", return_value=False):
        with pytest.raises(cost.UsageUnavailable):
            cost.today_usd(ACTOR, strict=True)
    with patch.object(cost.os, "makedirs", side_effect=OSError(30, "Read-only file system")):
        with pytest.raises(cost.UsageUnavailable):
            cost.today_usd(ACTOR, strict=True)


# ─── failed writes still count ───────────────────────────────────────────


def test_failed_github_write_is_counted_by_this_process(monkeypatch):
    _use_github(monkeypatch, _FakeRepo(stored=None, write_error=500))
    cost._bump_daily(ACTOR, 0.75)
    cost._bump_daily(ACTOR, 0.50)
    assert cost.today_usd(ACTOR, strict=True) == 1.25


def test_failed_local_write_is_counted_by_this_process():
    with patch.object(cost, "_bump_local", return_value=False):
        cost._bump_daily(ACTOR, 2.0)
    assert cost.today_usd(ACTOR, strict=True) == 2.0


def test_successful_write_is_not_double_counted(monkeypatch):
    _use_github(monkeypatch, _FakeRepo(stored=None))
    cost._bump_daily(ACTOR, 0.4)
    assert cost.today_usd(ACTOR, strict=True) == 0.4
    cost.configure("", "")
    cost._bump_daily(ACTOR, 0.3)
    assert cost.today_usd(ACTOR, strict=True) == 0.3


# ─── quota_blocked ───────────────────────────────────────────────────────


def test_quota_blocked_when_usage_cannot_be_read(monkeypatch):
    _use_github(monkeypatch, _FakeRepo(read_error=500))
    ctx = _ctx()
    with patch("api._bootstrap.audit.log"):
        blocked, _spent, quota = _bootstrap.quota_blocked(ctx)
    assert blocked is True
    assert ctx.usage_unavailable is True
    assert quota == 5.0


def test_quota_not_blocked_when_usage_is_readable(monkeypatch):
    _use_github(monkeypatch, _FakeRepo(stored={cost._today_key(): 1.0}))
    ctx = _ctx()
    with patch("api._bootstrap.audit.log"):
        blocked, spent, _quota = _bootstrap.quota_blocked(ctx)
    assert (blocked, spent, ctx.usage_unavailable) == (False, 1.0, False)


def test_quota_blocked_once_unpersisted_spend_reaches_the_cap(monkeypatch):
    _use_github(monkeypatch, _FakeRepo(stored=None, write_error=403))
    ctx = _ctx(quota=1.0)
    with patch("api._bootstrap.audit.log"):
        assert _bootstrap.quota_blocked(ctx)[0] is False
        cost._bump_daily(ACTOR, 1.0)
        assert _bootstrap.quota_blocked(ctx)[0] is True
    assert ctx.usage_unavailable is False


# ─── HTTP surface ────────────────────────────────────────────────────────


def test_generate_is_refused_when_usage_is_unavailable(monkeypatch):
    from fastapi.testclient import TestClient
    from api.index import app
    from headless_rate_limit import HTTP_RATE_LIMITER

    HTTP_RATE_LIMITER.reset()
    monkeypatch.setattr(_bootstrap, "_configured", True)  # keep the fake storage config
    _use_github(monkeypatch, _FakeRepo(read_error=500))
    with patch("api.index.core_service.generate") as gen, \
         patch("api._bootstrap.cost.wrap_client", side_effect=lambda c, _: c), \
         patch("api._bootstrap.audit.log"), \
         patch("api.index.audit.log"):
        r = TestClient(app).post(
            "/api/generate",
            headers={"X-API-Key": os.environ["TFGEN_API_KEY"]},
            json={"prompt": "create an Engineering group"},
        )
    assert r.status_code == 503, r.text
    assert "could not be verified" in r.json()["detail"]
    gen.assert_not_called()
