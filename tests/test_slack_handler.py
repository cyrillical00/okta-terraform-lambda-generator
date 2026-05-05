"""Tests for the Slack /tfgen handler in api/slack.py.

Standalone-runnable: `python tests/test_slack_handler.py` reports
PASS/FAIL per test without any pytest dependency. Pytest will also
discover these tests if installed (each test_* function uses bare
`assert`).

No real LLM calls: `core.service.generate` is monkey-patched. No real
Slack signature checks: `slack_sdk.signature.SignatureVerifier` is
patched per-test so we don't need a fixed canonical signature blob.

Background-task strategy: FastAPI's `BackgroundTasks.add_task` is
patched with a helper that captures the call (or runs it inline) so
the test can inspect what the background flow did with the mocked
downstream pipeline.
"""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Force known env before importing api/slack so the module's env reads
# at request time pick up our test values.
os.environ["SLACK_SIGNING_SECRET"] = "test-slack-secret-do-not-ship"
os.environ["SLACK_DEFAULT_REPO"] = "owner/test-repo"
os.environ["TFGEN_API_KEY"] = "test-secret-do-not-ship"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-fake"
os.environ.setdefault("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
os.environ.setdefault("TFGEN_HTTP_DAILY_QUOTA_USD", "5.00")
os.environ.setdefault("GITHUB_TOKEN", "ghp_fake_for_test")

from fastapi.testclient import TestClient

from core.service import GenerateResult


# ─── helpers ─────────────────────────────────────────────────────────────


def _client():
    """Late import so env-var overrides above land first."""
    from api.index import app
    # Touch api.slack so the route registers even if the import in
    # api/index.py was suppressed (test envs sometimes shadow imports).
    from api import slack  # noqa: F401
    return TestClient(app)


def _canned_outputs() -> dict[str, str]:
    return {
        "terraform_okta_hcl": 'resource "okta_group" "engineering" {\n  name = "Engineering"\n}\n',
        "terraform_lambda_hcl": "",
        "lambda_python": "",
        "lambda_requirements": "",
        "terraform_gcp_hcl": "",
        "cloud_function_python": "",
        "cloud_function_requirements": "",
        "terraform_tfvars_example": "",
    }


def _canned_intent() -> dict:
    return {
        "operation_type": "create",
        "resource_type": "okta_group",
        "resource_types": ["okta_group"],
        "resource_name": "Engineering",
        "output_mode": "Both",
    }


def _slack_form(text: str = "create an Engineering Okta group", **overrides) -> dict[str, str]:
    """Default slash-command form fields. Slack POSTs these as
    application/x-www-form-urlencoded."""
    base = {
        "command": "/tfgen",
        "text": text,
        "user_id": "U123ABC",
        "team_id": "T123ABC",
        "channel_id": "C123ABC",
        "response_url": "https://hooks.slack.com/commands/T123ABC/123/abc",
    }
    base.update(overrides)
    return base


def _patch_signature_valid():
    """Patch SignatureVerifier so .is_valid always returns True. Used by
    every test that wants to exercise the route past signature check."""
    fake = MagicMock()
    fake.is_valid.return_value = True
    return patch("slack_sdk.signature.SignatureVerifier", return_value=fake)


def _patch_signature_invalid():
    fake = MagicMock()
    fake.is_valid.return_value = False
    return patch("slack_sdk.signature.SignatureVerifier", return_value=fake)


def _fresh_timestamp() -> str:
    return str(int(time.time()))


def _stale_timestamp() -> str:
    # 10 minutes in the past, well past the 5-minute window.
    return str(int(time.time()) - 60 * 10)


def _capture_background(captured: dict):
    """Build a fake BackgroundTasks.add_task that just records the
    call. Returns a closure suitable for monkey-patching."""
    def fake_add_task(self, fn, *args, **kwargs):
        captured["fn"] = fn
        captured["args"] = args
        captured["kwargs"] = kwargs
    return fake_add_task


def _run_background_inline(captured: dict):
    """Build a fake BackgroundTasks.add_task that runs the function
    inline so its side effects are visible before the response
    returns to the test."""
    def fake_add_task(self, fn, *args, **kwargs):
        captured["fn"] = fn
        captured["args"] = args
        captured["kwargs"] = kwargs
        fn(*args, **kwargs)
    return fake_add_task


# ─── tests ───────────────────────────────────────────────────────────────


def test_valid_signature_populated_text_returns_working_on_it():
    """Happy synchronous path: valid signature, non-empty text -> 200,
    ephemeral response, background task scheduled."""
    captured = {}

    with _patch_signature_valid(), \
         patch("fastapi.BackgroundTasks.add_task", _capture_background(captured)):
        c = _client()
        r = c.post(
            "/api/slack/tfgen",
            data=_slack_form(),
            headers={
                "X-Slack-Signature": "v0=fake",
                "X-Slack-Request-Timestamp": _fresh_timestamp(),
            },
        )

    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body.get("response_type") == "ephemeral", f"expected ephemeral, got {body}"
    assert "Working on it" in body.get("text", ""), f"expected working-on-it, got {body}"
    assert "fn" in captured, "background task was not scheduled"
    assert captured["fn"].__name__ == "_run_generation", \
        f"expected _run_generation, got {captured['fn'].__name__}"


def test_bad_signature_returns_401():
    with _patch_signature_invalid():
        c = _client()
        r = c.post(
            "/api/slack/tfgen",
            data=_slack_form(),
            headers={
                "X-Slack-Signature": "v0=wrong",
                "X-Slack-Request-Timestamp": _fresh_timestamp(),
            },
        )
    assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"
    assert "signature" in r.json().get("detail", "").lower()


def test_stale_timestamp_returns_401():
    """A timestamp older than 5 minutes is rejected even when the
    signature would otherwise verify."""
    with _patch_signature_valid():
        c = _client()
        r = c.post(
            "/api/slack/tfgen",
            data=_slack_form(),
            headers={
                "X-Slack-Signature": "v0=fake",
                "X-Slack-Request-Timestamp": _stale_timestamp(),
            },
        )
    assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"
    assert "stale" in r.json().get("detail", "").lower()


def test_missing_text_returns_usage_hint():
    """Empty `text` returns 200 with usage hint and does NOT schedule
    a background task."""
    captured = {}

    with _patch_signature_valid(), \
         patch("fastapi.BackgroundTasks.add_task", _capture_background(captured)):
        c = _client()
        r = c.post(
            "/api/slack/tfgen",
            data=_slack_form(text=""),
            headers={
                "X-Slack-Signature": "v0=fake",
                "X-Slack-Request-Timestamp": _fresh_timestamp(),
            },
        )

    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body.get("response_type") == "ephemeral"
    assert "Usage" in body.get("text", "") or "usage" in body.get("text", "").lower()
    assert "fn" not in captured, "background task should NOT run on empty text"


def test_empty_body_returns_200_with_usage_hint():
    """An empty form body -> empty `text` -> usage-hint path. We chose
    200 over 422 so Slack doesn't show its own generic error message;
    the user gets the same usage hint instead."""
    with _patch_signature_valid():
        c = _client()
        r = c.post(
            "/api/slack/tfgen",
            data={},  # nothing at all
            headers={
                "X-Slack-Signature": "v0=fake",
                "X-Slack-Request-Timestamp": _fresh_timestamp(),
            },
        )
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body.get("response_type") == "ephemeral"
    assert "usage" in body.get("text", "").lower()


def test_missing_signing_secret_returns_503():
    """Server with no SLACK_SIGNING_SECRET returns 503; same shape as
    the HTTP API's TFGEN_API_KEY-missing case."""
    with patch.dict(os.environ, {"SLACK_SIGNING_SECRET": ""}, clear=False):
        c = _client()
        r = c.post(
            "/api/slack/tfgen",
            data=_slack_form(),
            headers={
                "X-Slack-Signature": "v0=fake",
                "X-Slack-Request-Timestamp": _fresh_timestamp(),
            },
        )
    assert r.status_code == 503, f"expected 503, got {r.status_code}: {r.text}"
    assert "SLACK_SIGNING_SECRET" in r.json().get("detail", "")


def test_missing_default_repo_returns_ephemeral_error():
    """When SLACK_DEFAULT_REPO is unset, the synchronous response
    surfaces the misconfiguration. We return 200 (not 503) so Slack
    actually shows the error to the user instead of a generic failure."""
    with _patch_signature_valid(), \
         patch.dict(os.environ, {"SLACK_DEFAULT_REPO": ""}, clear=False):
        c = _client()
        r = c.post(
            "/api/slack/tfgen",
            data=_slack_form(),
            headers={
                "X-Slack-Signature": "v0=fake",
                "X-Slack-Request-Timestamp": _fresh_timestamp(),
            },
        )
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body.get("response_type") == "ephemeral"
    assert "SLACK_DEFAULT_REPO" in body.get("text", "")


def test_background_task_runs_full_pipeline_and_posts_to_response_url():
    """End-to-end verification of the background flow:
       - core.service.generate is called with the redacted prompt
       - push_to_github is called with the file map
       - requests.post is called against response_url with the commit URL
    """
    canned_result = GenerateResult(
        intent=_canned_intent(),
        outputs=_canned_outputs(),
        validation_result={"terraform_issues": [], "lambda_issues": []},
    )
    fake_commit = "https://github.com/owner/test-repo/commit/deadbeef"

    captured_generate = {}
    captured_post = {}
    captured_bg = {}

    def fake_generate(prompt, **kwargs):
        captured_generate["prompt"] = prompt
        captured_generate["kwargs"] = kwargs
        return canned_result

    def fake_post(url, json=None, timeout=None):
        captured_post["url"] = url
        captured_post["json"] = json
        return MagicMock(status_code=200)

    with _patch_signature_valid(), \
         patch("fastapi.BackgroundTasks.add_task", _run_background_inline(captured_bg)), \
         patch("api.slack.core_service.generate", side_effect=fake_generate), \
         patch("api.slack.push_to_github", return_value=fake_commit), \
         patch("api.slack.requests.post", side_effect=fake_post), \
         patch("api.slack.cost.today_usd", return_value=0.05), \
         patch("api._bootstrap.cost.today_usd", return_value=0.05), \
         patch("api._bootstrap.cost.wrap_client", side_effect=lambda c, _: c), \
         patch("api.slack.audit.log"), \
         patch("api._bootstrap.audit.log"):
        c = _client()
        r = c.post(
            "/api/slack/tfgen",
            data=_slack_form(text="create an Engineering Okta group"),
            headers={
                "X-Slack-Signature": "v0=fake",
                "X-Slack-Request-Timestamp": _fresh_timestamp(),
            },
        )

    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    assert captured_generate, "core.service.generate was not called"
    # Redaction is a no-op on this benign prompt; assert the prompt
    # round-trips intact.
    assert "Engineering" in captured_generate["prompt"], (
        f"expected redacted prompt to contain 'Engineering', got {captured_generate['prompt']!r}"
    )
    assert captured_post.get("url", "").startswith("https://hooks.slack.com/"), \
        f"response_url post target wrong: {captured_post.get('url')!r}"
    posted = captured_post.get("json") or {}
    assert posted.get("response_type") == "in_channel", \
        f"expected in_channel on success, got {posted}"
    assert fake_commit in posted.get("text", ""), \
        f"expected commit URL in posted text, got {posted}"
    assert "okta_group" in posted.get("text", ""), \
        "expected fenced okta.tf preview in posted text"


def test_background_task_quota_blocked_posts_quota_message():
    """When today's spend exceeds the quota, the background task posts
    a quota-exhausted message to response_url and skips generation."""
    captured_post = {}
    captured_bg = {}

    def fake_post(url, json=None, timeout=None):
        captured_post["json"] = json
        return MagicMock(status_code=200)

    generate_called = {"count": 0}

    def fake_generate(*a, **kw):
        generate_called["count"] += 1
        return GenerateResult(intent={}, outputs={})

    with _patch_signature_valid(), \
         patch("fastapi.BackgroundTasks.add_task", _run_background_inline(captured_bg)), \
         patch("api.slack.core_service.generate", side_effect=fake_generate), \
         patch("api.slack.requests.post", side_effect=fake_post), \
         patch("api._bootstrap.cost.today_usd", return_value=999.99), \
         patch("api.slack.cost.today_usd", return_value=999.99), \
         patch("api._bootstrap.cost.wrap_client", side_effect=lambda c, _: c), \
         patch("api.slack.audit.log"), \
         patch("api._bootstrap.audit.log"):
        c = _client()
        r = c.post(
            "/api/slack/tfgen",
            data=_slack_form(),
            headers={
                "X-Slack-Signature": "v0=fake",
                "X-Slack-Request-Timestamp": _fresh_timestamp(),
            },
        )

    assert r.status_code == 200
    assert generate_called["count"] == 0, "generate must NOT run when quota is blocked"
    posted = captured_post.get("json") or {}
    assert "quota" in posted.get("text", "").lower(), \
        f"expected quota message, got {posted}"


def test_background_task_push_failure_falls_back_to_files_message():
    """If push_to_github raises RuntimeError, the user still gets the
    generated files as a fenced code block via response_url."""
    canned_result = GenerateResult(
        intent=_canned_intent(),
        outputs=_canned_outputs(),
        validation_result={"terraform_issues": [], "lambda_issues": []},
    )
    captured_post = {}
    captured_bg = {}

    def fake_post(url, json=None, timeout=None):
        captured_post["json"] = json
        return MagicMock(status_code=200)

    with _patch_signature_valid(), \
         patch("fastapi.BackgroundTasks.add_task", _run_background_inline(captured_bg)), \
         patch("api.slack.core_service.generate", return_value=canned_result), \
         patch("api.slack.push_to_github", side_effect=RuntimeError("Repository 'owner/test-repo' not found.")), \
         patch("api.slack.requests.post", side_effect=fake_post), \
         patch("api._bootstrap.cost.today_usd", return_value=0.05), \
         patch("api.slack.cost.today_usd", return_value=0.05), \
         patch("api._bootstrap.cost.wrap_client", side_effect=lambda c, _: c), \
         patch("api.slack.audit.log"), \
         patch("api._bootstrap.audit.log"):
        c = _client()
        r = c.post(
            "/api/slack/tfgen",
            data=_slack_form(),
            headers={
                "X-Slack-Signature": "v0=fake",
                "X-Slack-Request-Timestamp": _fresh_timestamp(),
            },
        )

    assert r.status_code == 200
    posted = captured_post.get("json") or {}
    assert "push failed" in posted.get("text", "").lower(), \
        f"expected push-failure message, got {posted}"
    assert "okta_group" in posted.get("text", ""), \
        "expected file content in fallback message"


def test_actor_id_is_stable_for_slack_user():
    """Same Slack user id always hashes to the same actor_id."""
    from api.slack import _actor_id_for_slack_user
    a = _actor_id_for_slack_user("U123ABC")
    b = _actor_id_for_slack_user("U123ABC")
    c = _actor_id_for_slack_user("U999XYZ")
    assert a == b
    assert a != c
    assert len(a) == 16


# ─── runner for standalone invocation ────────────────────────────────────


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(0 if failures == 0 else 1)
