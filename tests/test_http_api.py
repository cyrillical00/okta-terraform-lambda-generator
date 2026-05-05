"""Tests for the HTTP API in api/index.py.

Standalone-runnable: `python tests/test_http_api.py` reports PASS/FAIL
per test without any pytest dependency. Pytest will also discover these
tests if installed (each test_* function uses bare `assert`).

No real LLM calls — `core.service.generate` is monkey-patched to return
a canned GenerateResult, and `cost.wrap_client` is short-circuited so
nothing tries to actually configure GitHub or accumulate usage.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Force a known TFGEN_API_KEY before any api/* import so _auth's
# verify_api_key compares against it. We restore in tearDown via the
# context manager.
os.environ["TFGEN_API_KEY"] = "test-secret-do-not-ship"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-fake"  # bypasses _bootstrap raise
os.environ.setdefault("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
os.environ.setdefault("TFGEN_HTTP_DAILY_QUOTA_USD", "5.00")

from fastapi.testclient import TestClient

from core.service import GenerateResult


def _client():
    """Late import so env-var overrides above land first."""
    from api.index import app
    return TestClient(app)


def _canned_outputs() -> dict[str, str]:
    return {
        "terraform_okta_hcl": 'resource "okta_group" "engineering" {\n  name = "Engineering"\n}\n',
        "terraform_lambda_hcl": "",
        "lambda_python": "def handler(event, context):\n    return {}\n",
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


def test_health_no_auth():
    """GET /api/health returns 200 with status ok and no auth required."""
    c = _client()
    r = c.get("/api/health")
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body.get("status") == "ok", f"expected status=ok, got {body}"


def test_generate_missing_api_key_returns_401():
    c = _client()
    r = c.post("/api/generate", json={"prompt": "create an Engineering group"})
    assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"


def test_generate_wrong_api_key_returns_401():
    c = _client()
    r = c.post(
        "/api/generate",
        headers={"X-API-Key": "wrong-key"},
        json={"prompt": "create an Engineering group"},
    )
    assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"


def test_generate_empty_body_returns_422():
    """Pydantic should reject a missing prompt with 422."""
    c = _client()
    r = c.post(
        "/api/generate",
        headers={"X-API-Key": os.environ["TFGEN_API_KEY"]},
        json={},
    )
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"


def test_generate_empty_prompt_returns_422():
    c = _client()
    r = c.post(
        "/api/generate",
        headers={"X-API-Key": os.environ["TFGEN_API_KEY"]},
        json={"prompt": ""},
    )
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"


def test_generate_happy_path_returns_files():
    """Full /api/generate flow with mocked service.generate. Verifies
    the file map is built correctly and the response shape matches."""
    canned = GenerateResult(
        intent=_canned_intent(),
        outputs=_canned_outputs(),
        validation_result={"terraform_issues": [], "lambda_issues": []},
    )
    with patch("core.service.generate", return_value=canned), \
         patch("api.index.core_service.generate", return_value=canned), \
         patch("api._bootstrap.cost.wrap_client", side_effect=lambda c, _: c), \
         patch("api._bootstrap.cost.today_usd", return_value=0.05), \
         patch("api.index.cost.today_usd", return_value=0.05), \
         patch("api._bootstrap.audit.log"), \
         patch("api.index.audit.log"):
        c = _client()
        r = c.post(
            "/api/generate",
            headers={"X-API-Key": os.environ["TFGEN_API_KEY"]},
            json={"prompt": "create an Engineering group", "output_mode": "Both"},
        )
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body.get("intent", {}).get("resource_name") == "Engineering"
    files = body.get("files") or {}
    assert "terraform/okta.tf" in files, f"expected terraform/okta.tf in files, got {list(files)}"
    assert files["terraform/okta.tf"].startswith('resource "okta_group"'), "expected okta_group resource block"


def test_generate_quota_blocked_returns_429():
    """When today's spend exceeds the quota, the endpoint short-circuits
    to 429 before ever calling core.service.generate."""
    with patch("api._bootstrap.cost.today_usd", return_value=999.99), \
         patch("api._bootstrap.audit.log"), \
         patch("api.index.audit.log"):
        c = _client()
        r = c.post(
            "/api/generate",
            headers={"X-API-Key": os.environ["TFGEN_API_KEY"]},
            json={"prompt": "anything"},
        )
    assert r.status_code == 429, f"expected 429, got {r.status_code}: {r.text}"


def test_push_missing_api_key_returns_401():
    c = _client()
    r = c.post("/api/push", json={
        "files": {"a.tf": "resource \"x\" \"y\" {}"},
        "repo": "owner/repo",
        "commit_message": "test",
    })
    assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"


def test_push_happy_path_returns_commit_url():
    fake_url = "https://github.com/owner/repo/commit/abcdef"
    with patch("api.index.push_to_github", return_value=fake_url), \
         patch("api.index.audit.log"), \
         patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fake_for_test"}):
        c = _client()
        r = c.post(
            "/api/push",
            headers={"X-API-Key": os.environ["TFGEN_API_KEY"]},
            json={
                "files": {"terraform/okta.tf": "resource \"okta_group\" \"x\" { name = \"X\" }"},
                "repo": "owner/repo",
                "branch": "feature/test",
                "commit_message": "feat: test push",
            },
        )
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    assert r.json().get("commit_url") == fake_url


def test_push_runtime_error_returns_400():
    """push_to_github raises RuntimeError with human-readable messages
    (repo not found, branch can't be created, empty repo). Surface as
    400 so the caller can fix their request."""
    with patch("api.index.push_to_github", side_effect=RuntimeError("Repository 'owner/missing' not found.")), \
         patch("api.index.audit.log"), \
         patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fake_for_test"}):
        c = _client()
        r = c.post(
            "/api/push",
            headers={"X-API-Key": os.environ["TFGEN_API_KEY"]},
            json={
                "files": {"a.tf": "x"},
                "repo": "owner/missing",
                "commit_message": "test",
            },
        )
    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
    assert "not found" in r.json().get("detail", "").lower()


def test_auth_actor_id_is_stable():
    """Same key always hashes to the same actor_id. Different keys
    produce different ids. Sanity check the hashing layer that audit
    + cost rely on."""
    from api._auth import actor_id_for
    a = actor_id_for("k1")
    b = actor_id_for("k1")
    c = actor_id_for("k2")
    assert a == b, "same key must hash to same actor_id"
    assert a != c, "different keys must hash to different ids"
    assert len(a) == 16, f"expected 16-char hash, got {len(a)}"


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
