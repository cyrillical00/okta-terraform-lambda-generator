"""JIRA webhook: where callback credentials may go, issue-key checks and
replay handling. Nothing here talks to JIRA, GitHub or the model.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("JIRA_WEBHOOK_SECRET", "test-jira-secret-do-not-ship")
os.environ.setdefault("JIRA_DEFAULT_REPO", "owner/test-repo")
os.environ.setdefault("JIRA_USER_EMAIL", "bot@test.local")
os.environ.setdefault("JIRA_API_TOKEN", "test-token")
os.environ.setdefault("GITHUB_TOKEN", "ghp_fake_for_test")
os.environ.setdefault("TFGEN_API_KEY", "test-secret-do-not-ship")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-fake")

from fastapi.testclient import TestClient

from api.index import app
import api.jira as jira
from core.service import GenerateResult
from headless_rate_limit import JIRA_RATE_LIMITER

FAKE_COMMIT = "https://github.com/owner/test-repo/commit/abcdef"


def _payload(issue_key="PROJ-123", self_url=None, **top_level):
    return {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": issue_key,
            "self": self_url or f"https://company.atlassian.net/rest/api/3/issue/{issue_key}",
            "fields": {
                "summary": "Create an Engineering Okta group",
                "description": "Need an okta_group named Engineering.",
                "labels": ["tfgen"],
                "creator": {"emailAddress": "alice@example.com", "accountId": "acct-123"},
            },
        },
        **top_level,
    }


def _deliver(client, payload):
    body = json.dumps(payload).encode("utf-8")
    mac = hmac.new(os.environ["JIRA_WEBHOOK_SECRET"].encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/api/jira/webhook",
        content=body,
        headers={"X-Hub-Signature": f"sha256={mac}", "Content-Type": "application/json"},
    )


@contextlib.contextmanager
def _pipeline():
    """Stub generate/push/JIRA REST and hand back the mocks."""
    canned = GenerateResult(
        intent={"resource_type": "okta_group"},
        outputs={"terraform_okta_hcl": 'resource "okta_group" "e" { name = "E" }'},
        validation_result={},
    )
    ok = MagicMock(status_code=201, text="")
    with patch("api.jira.core_service.generate", return_value=canned) as gen, \
         patch("api.jira.push_to_github", return_value=FAKE_COMMIT) as push, \
         patch("api.jira.requests.post", return_value=ok) as post, \
         patch("api._bootstrap.cost.wrap_client", side_effect=lambda c, _: c), \
         patch("api._bootstrap.cost.today_usd", return_value=0.05), \
         patch("api.jira.cost.today_usd", return_value=0.05), \
         patch("api._bootstrap.audit.log"), \
         patch("api.jira.audit.log"):
        yield gen, push, post


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    jira._reset_replay_state_for_tests()
    JIRA_RATE_LIMITER.reset()
    yield TestClient(app)
    jira._reset_replay_state_for_tests()
    JIRA_RATE_LIMITER.reset()


# ─── where the callback credentials go ───────────────────────────────────


@pytest.mark.parametrize("self_url", [
    "https://attacker.example/rest/api/3/issue/PROJ-123",
    "https://company.atlassian.net.attacker.example/rest/api/3/issue/PROJ-123",
    "https://company.atlassian.net@attacker.example/rest/api/3/issue/PROJ-123",
    "https://attacker.example/company.atlassian.net/rest/api/3/issue/PROJ-123",
    "http://company.atlassian.net/rest/api/3/issue/PROJ-123",
    "https://company.atlassian.net:8443/rest/api/3/issue/PROJ-123",
    "https://user:pw@company.atlassian.net/rest/api/3/issue/PROJ-123",
])
def test_credentials_are_not_sent_to_a_host_named_by_the_payload(client, self_url):
    assert jira._jira_base_url(self_url) == ""
    with _pipeline() as (gen, push, post):
        r = _deliver(client, _payload(self_url=self_url))
    assert r.status_code == 200, r.text
    post.assert_not_called()


def test_jira_cloud_host_is_accepted_without_configuration(client):
    with _pipeline() as (gen, push, post):
        r = _deliver(client, _payload())
    assert r.json().get("status") == "ok", r.text
    assert post.call_args.args[0] == "https://company.atlassian.net/rest/api/3/issue/PROJ-123/comment"


def test_configured_base_url_overrides_the_payload(client, monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.internal.example/")
    with _pipeline() as (gen, push, post):
        r = _deliver(client, _payload(self_url="https://attacker.example/rest/api/3/issue/PROJ-123"))
    assert r.json().get("status") == "ok", r.text
    assert post.call_args.args[0] == "https://jira.internal.example/rest/api/3/issue/PROJ-123/comment"
    assert all("attacker.example" not in str(c) for c in post.call_args_list)


def test_a_non_https_configured_base_url_disables_the_callback(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "http://jira.internal.example")
    assert jira._jira_base_url("https://company.atlassian.net/rest/api/3/issue/X-1") == ""


# ─── issue key ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("issue_key", [
    "PROJ-1/../../main", "../../etc", "PROJ-1 OR 1=1", "proj-1", "PROJ", "-1", "PROJ-", "PROJ-1\nX", "",
])
def test_malformed_issue_keys_do_no_work(client, issue_key):
    with _pipeline() as (gen, push, post):
        r = _deliver(client, _payload(issue_key=issue_key))
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ignored", "reason": "invalid issue key"}
    gen.assert_not_called()
    push.assert_not_called()
    post.assert_not_called()


# ─── replay ──────────────────────────────────────────────────────────────


def test_the_same_signed_delivery_is_processed_once(client):
    payload = _payload()
    with _pipeline() as (gen, push, post):
        first = _deliver(client, payload)
        second = _deliver(client, payload)
        third = _deliver(client, payload)
    assert first.json().get("status") == "ok", first.text
    assert second.json() == {"status": "ignored", "reason": "duplicate delivery"}
    assert third.json() == {"status": "ignored", "reason": "duplicate delivery"}
    assert gen.call_count == 1
    assert push.call_count == 1


def test_a_different_delivery_for_the_same_issue_is_still_processed(client):
    now_ms = int(time.time() * 1000)
    with _pipeline() as (gen, push, post):
        a = _deliver(client, _payload(timestamp=now_ms))
        b = _deliver(client, _payload(timestamp=now_ms + 1))
    assert a.json().get("status") == "ok" and b.json().get("status") == "ok"
    assert gen.call_count == 2


def test_duplicates_are_forgotten_after_the_window(client):
    payload = _payload()
    with _pipeline() as (gen, push, post):
        _deliver(client, payload)
        with patch.object(jira.time, "monotonic", return_value=time.monotonic() + jira._REPLAY_WINDOW_SECONDS + 1):
            again = _deliver(client, payload)
    assert again.json().get("status") == "ok", again.text
    assert gen.call_count == 2


@pytest.mark.parametrize("age_seconds", [jira._REPLAY_WINDOW_SECONDS + 60, 86_400, -(jira._FUTURE_SKEW_SECONDS + 60)])
def test_stale_or_future_dated_deliveries_are_ignored(client, age_seconds):
    stamp_ms = int((time.time() - age_seconds) * 1000)
    with _pipeline() as (gen, push, post):
        r = _deliver(client, _payload(timestamp=stamp_ms))
    assert r.json() == {"status": "ignored", "reason": "stale delivery"}
    gen.assert_not_called()


def test_fresh_timestamp_in_seconds_or_milliseconds_is_accepted(client):
    with _pipeline() as (gen, push, post):
        a = _deliver(client, _payload(timestamp=int(time.time() * 1000)))
        b = _deliver(client, _payload(timestamp=int(time.time()) - 5))
    assert a.json().get("status") == "ok" and b.json().get("status") == "ok"


def test_ignored_events_are_not_recorded_as_deliveries(client):
    """An unlabeled event must not block the later, labeled one."""
    unlabeled = _payload()
    unlabeled["issue"]["fields"]["labels"] = []
    with _pipeline() as (gen, push, post):
        assert _deliver(client, unlabeled).json().get("status") == "ignored"
        r = _deliver(client, _payload())
    assert r.json().get("status") == "ok", r.text
