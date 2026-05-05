"""Tests for the JIRA webhook handler in api/jira.py.

Standalone-runnable: `python tests/test_jira_handler.py` reports
PASS/FAIL per test without any pytest dependency. Pytest will also
discover these tests if installed.

No real LLM calls and no real JIRA REST calls. core.service.generate
is monkey-patched, requests.post is mocked, push_to_github is mocked,
and cost.wrap_client is short-circuited so nothing tries to actually
configure GitHub or accumulate usage.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Force known env values before any api/* import so verify_jira_signature
# and the bootstrap module read them on first call.
os.environ["JIRA_WEBHOOK_SECRET"] = "test-jira-secret-do-not-ship"
os.environ["JIRA_DEFAULT_REPO"] = "owner/test-repo"
os.environ["JIRA_USER_EMAIL"] = "bot@test.local"
os.environ["JIRA_API_TOKEN"] = "test-token"
os.environ["GITHUB_TOKEN"] = "ghp_fake_for_test"
os.environ.setdefault("TFGEN_API_KEY", "test-secret-do-not-ship")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-fake")
os.environ.setdefault("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
os.environ.setdefault("TFGEN_HTTP_DAILY_QUOTA_USD", "5.00")

from fastapi.testclient import TestClient

from core.service import GenerateResult


# ─── helpers ─────────────────────────────────────────────────────────────


def _client():
    """Late import so env-var overrides above land first."""
    from api.index import app
    import api.jira  # noqa: F401  (registers /api/jira/webhook)
    return TestClient(app)


def _sign(body: bytes, secret: str | None = None) -> str:
    """Compute the X-Hub-Signature header value for a body + secret."""
    s = secret if secret is not None else os.environ["JIRA_WEBHOOK_SECRET"]
    mac = hmac.new(s.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def _payload(
    *,
    event: str = "jira:issue_created",
    issue_key: str = "PROJ-123",
    summary: str = "Create an Engineering Okta group",
    description: object = "Need an okta_group named Engineering for the new team.",
    labels: list[str] | None = None,
    creator_email: str = "alice@example.com",
    self_url: str | None = None,
) -> dict:
    if labels is None:
        labels = ["tfgen"]
    if self_url is None:
        self_url = f"https://company.atlassian.net/rest/api/3/issue/{issue_key}"
    fields: dict = {
        "summary": summary,
        "description": description,
        "labels": labels,
        "creator": {"emailAddress": creator_email, "accountId": "acct-123"},
    }
    return {
        "webhookEvent": event,
        "issue": {
            "key": issue_key,
            "self": self_url,
            "fields": fields,
        },
    }


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


def _ok_response(status: int = 201) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.text = ""
    return m


# ─── tests ───────────────────────────────────────────────────────────────


def test_valid_hmac_with_tfgen_label_triggers_generation_and_push():
    """Valid signature + tfgen label + jira:issue_created should run the
    full pipeline: redact, generate, push, and post a comment."""
    canned = GenerateResult(
        intent=_canned_intent(),
        outputs=_canned_outputs(),
        validation_result={"terraform_issues": [], "lambda_issues": []},
    )
    body = json.dumps(_payload()).encode("utf-8")
    fake_url = "https://github.com/owner/test-repo/commit/abcdef"

    with patch("api.jira.core_service.generate", return_value=canned), \
         patch("api.jira.push_to_github", return_value=fake_url) as push_mock, \
         patch("api.jira.requests.post", return_value=_ok_response()) as post_mock, \
         patch("api._bootstrap.cost.wrap_client", side_effect=lambda c, _: c), \
         patch("api._bootstrap.cost.today_usd", return_value=0.05), \
         patch("api.jira.cost.today_usd", return_value=0.05), \
         patch("api._bootstrap.audit.log"), \
         patch("api.jira.audit.log"):
        c = _client()
        r = c.post(
            "/api/jira/webhook",
            content=body,
            headers={
                "X-Hub-Signature": _sign(body),
                "Content-Type": "application/json",
            },
        )

    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    body_json = r.json()
    assert body_json.get("status") == "ok", f"expected status=ok, got {body_json}"
    assert body_json.get("commit_url") == fake_url
    assert body_json.get("issue_key") == "PROJ-123"
    assert body_json.get("branch") == "jira/PROJ-123"

    # push was called with the right repo and branch
    assert push_mock.called, "push_to_github should have been called"
    push_args, push_kwargs = push_mock.call_args
    assert push_args[1] == "owner/test-repo", f"expected repo=owner/test-repo, got {push_args[1]}"
    assert push_kwargs.get("branch") == "jira/PROJ-123"

    # comment was posted to JIRA
    assert post_mock.called, "requests.post should have been called for the JIRA comment"
    call_url = post_mock.call_args.args[0] if post_mock.call_args.args else post_mock.call_args.kwargs.get("url", "")
    assert "/rest/api/3/issue/PROJ-123/comment" in call_url, f"expected comment URL, got {call_url}"


def test_valid_hmac_no_tfgen_label_returns_ignored():
    """Missing tfgen label should short-circuit to 200 ignored without
    invoking the generator."""
    body = json.dumps(_payload(labels=["bug", "frontend"])).encode("utf-8")
    gen_mock = MagicMock()
    with patch("api.jira.core_service.generate", gen_mock), \
         patch("api.jira.push_to_github") as push_mock, \
         patch("api.jira.requests.post") as post_mock:
        c = _client()
        r = c.post(
            "/api/jira/webhook",
            content=body,
            headers={"X-Hub-Signature": _sign(body), "Content-Type": "application/json"},
        )
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    assert r.json().get("status") == "ignored"
    assert "tfgen" in r.json().get("reason", "")
    assert not gen_mock.called, "generate should not run for non-tfgen issues"
    assert not push_mock.called
    assert not post_mock.called


def test_bad_hmac_signature_returns_401():
    body = json.dumps(_payload()).encode("utf-8")
    c = _client()
    r = c.post(
        "/api/jira/webhook",
        content=body,
        headers={
            "X-Hub-Signature": _sign(body, secret="wrong-secret"),
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"


def test_missing_signature_header_returns_401():
    body = json.dumps(_payload()).encode("utf-8")
    c = _client()
    r = c.post(
        "/api/jira/webhook",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"


def test_issue_deleted_event_returns_ignored():
    body = json.dumps(_payload(event="jira:issue_deleted")).encode("utf-8")
    gen_mock = MagicMock()
    with patch("api.jira.core_service.generate", gen_mock):
        c = _client()
        r = c.post(
            "/api/jira/webhook",
            content=body,
            headers={"X-Hub-Signature": _sign(body), "Content-Type": "application/json"},
        )
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    assert r.json().get("status") == "ignored"
    assert not gen_mock.called


def test_empty_description_uses_summary_only():
    """When description is empty, the prompt passed to generate should
    be just the summary."""
    canned = GenerateResult(intent=_canned_intent(), outputs=_canned_outputs())
    body = json.dumps(_payload(summary="just create an okta group", description="")).encode("utf-8")
    fake_url = "https://github.com/owner/test-repo/commit/xyz"

    captured = {}

    def _capture_generate(prompt, **kwargs):
        captured["prompt"] = prompt
        return canned

    with patch("api.jira.core_service.generate", side_effect=_capture_generate), \
         patch("api.jira.push_to_github", return_value=fake_url), \
         patch("api.jira.requests.post", return_value=_ok_response()), \
         patch("api._bootstrap.cost.wrap_client", side_effect=lambda c, _: c), \
         patch("api._bootstrap.cost.today_usd", return_value=0.05), \
         patch("api.jira.cost.today_usd", return_value=0.05), \
         patch("api._bootstrap.audit.log"), \
         patch("api.jira.audit.log"):
        c = _client()
        r = c.post(
            "/api/jira/webhook",
            content=body,
            headers={"X-Hub-Signature": _sign(body), "Content-Type": "application/json"},
        )

    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    assert "prompt" in captured, "generate was not called"
    # Prompt should be just the summary (no description suffix), with no
    # double-newline tail.
    assert "just create an okta group" in captured["prompt"]
    assert "\n\n" not in captured["prompt"], f"expected no description suffix, got: {captured['prompt']!r}"


def test_missing_jira_webhook_secret_returns_503():
    """When the env var is unset, the verifier returns 503 (server
    misconfiguration, not a bad signature)."""
    body = json.dumps(_payload()).encode("utf-8")
    with patch.dict(os.environ, {"JIRA_WEBHOOK_SECRET": ""}, clear=False):
        c = _client()
        r = c.post(
            "/api/jira/webhook",
            content=body,
            headers={"X-Hub-Signature": _sign(body), "Content-Type": "application/json"},
        )
    assert r.status_code == 503, f"expected 503, got {r.status_code}: {r.text}"


def test_adf_description_is_extracted_to_text():
    """When description arrives as Atlassian Document Format JSON, the
    handler should walk it and extract plaintext for the prompt."""
    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Provision "},
                    {"type": "text", "text": "an Engineering group"},
                ],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "with SSO via Okta."}],
            },
        ],
    }
    canned = GenerateResult(intent=_canned_intent(), outputs=_canned_outputs())
    body = json.dumps(_payload(description=adf)).encode("utf-8")
    fake_url = "https://github.com/owner/test-repo/commit/abc"

    captured = {}

    def _capture_generate(prompt, **kwargs):
        captured["prompt"] = prompt
        return canned

    with patch("api.jira.core_service.generate", side_effect=_capture_generate), \
         patch("api.jira.push_to_github", return_value=fake_url), \
         patch("api.jira.requests.post", return_value=_ok_response()), \
         patch("api._bootstrap.cost.wrap_client", side_effect=lambda c, _: c), \
         patch("api._bootstrap.cost.today_usd", return_value=0.05), \
         patch("api.jira.cost.today_usd", return_value=0.05), \
         patch("api._bootstrap.audit.log"), \
         patch("api.jira.audit.log"):
        c = _client()
        r = c.post(
            "/api/jira/webhook",
            content=body,
            headers={"X-Hub-Signature": _sign(body), "Content-Type": "application/json"},
        )

    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    prompt = captured.get("prompt", "")
    assert "Provision an Engineering group" in prompt, f"ADF text not extracted, got: {prompt!r}"
    assert "with SSO via Okta." in prompt, f"second paragraph missing, got: {prompt!r}"


def test_generation_error_posts_jira_comment():
    """When generate returns an error, a JIRA comment with the error
    should be posted and the response should be 200 (not 5xx) so JIRA
    does not retry."""
    canned_err = GenerateResult(
        intent={"resource_type": ""},
        outputs=None,
        error="model returned malformed JSON",
    )
    body = json.dumps(_payload()).encode("utf-8")

    with patch("api.jira.core_service.generate", return_value=canned_err), \
         patch("api.jira.push_to_github") as push_mock, \
         patch("api.jira.requests.post", return_value=_ok_response()) as post_mock, \
         patch("api._bootstrap.cost.wrap_client", side_effect=lambda c, _: c), \
         patch("api._bootstrap.cost.today_usd", return_value=0.05), \
         patch("api.jira.cost.today_usd", return_value=0.05), \
         patch("api._bootstrap.audit.log"), \
         patch("api.jira.audit.log"):
        c = _client()
        r = c.post(
            "/api/jira/webhook",
            content=body,
            headers={"X-Hub-Signature": _sign(body), "Content-Type": "application/json"},
        )

    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    assert r.json().get("status") == "generation_failed"
    assert not push_mock.called, "push should be skipped when generation fails"
    assert post_mock.called, "JIRA comment should be posted on generation failure"
    # The posted comment should contain the error.
    posted_kwargs = post_mock.call_args.kwargs
    body_field = posted_kwargs.get("json", {}).get("body", "")
    assert "malformed JSON" in body_field, f"expected error in comment, got: {body_field!r}"


def test_push_failure_posts_files_inline_comment():
    """When push raises RuntimeError, the JIRA comment should include
    the error and the raw files so the user can salvage."""
    canned = GenerateResult(intent=_canned_intent(), outputs=_canned_outputs())
    body = json.dumps(_payload()).encode("utf-8")

    with patch("api.jira.core_service.generate", return_value=canned), \
         patch("api.jira.push_to_github", side_effect=RuntimeError("Repository 'owner/test-repo' not found.")), \
         patch("api.jira.requests.post", return_value=_ok_response()) as post_mock, \
         patch("api._bootstrap.cost.wrap_client", side_effect=lambda c, _: c), \
         patch("api._bootstrap.cost.today_usd", return_value=0.05), \
         patch("api.jira.cost.today_usd", return_value=0.05), \
         patch("api._bootstrap.audit.log"), \
         patch("api.jira.audit.log"):
        c = _client()
        r = c.post(
            "/api/jira/webhook",
            content=body,
            headers={"X-Hub-Signature": _sign(body), "Content-Type": "application/json"},
        )

    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    assert r.json().get("status") == "push_failed"
    assert post_mock.called, "JIRA comment should be posted on push failure"
    posted_kwargs = post_mock.call_args.kwargs
    body_field = posted_kwargs.get("json", {}).get("body", "")
    assert "not found" in body_field.lower(), f"expected error in comment, got: {body_field[:200]!r}"
    # Raw files should be inlined for salvage.
    assert "okta_group" in body_field, f"expected inline file content, got: {body_field[:200]!r}"


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
