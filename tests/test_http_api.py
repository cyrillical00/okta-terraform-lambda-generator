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


# ─── Phase 21a: per-key tokens via [api_keys] in roles.toml ──────────────


def _write_roles_toml_with_keys(tmp_path, entries: list[dict]) -> str:
    """Write a roles.toml with the given [api_keys.*] entries and return its path."""
    import hashlib
    lines = ["[cost]", "daily_cap_usd = 5.00", ""]
    for i, e in enumerate(entries):
        slug = e.get("slug") or f"entry{i}"
        plaintext = e["plaintext"]
        digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
        lines.append(f"[api_keys.{slug}]")
        lines.append(f'sha256          = "{digest}"')
        lines.append(f'actor_id        = "{e["actor_id"]}"')
        lines.append(f'role            = "{e["role"]}"')
        lines.append(f"daily_quota_usd = {e.get('quota', 1.0)}")
        lines.append("")
    p = tmp_path / "roles.toml"
    p.write_text("\n".join(lines), encoding="utf-8")
    return str(p)


def test_per_key_token_resolves_to_configured_actor_id(tmp_path, monkeypatch):
    """A token whose hash is in roles.toml resolves to the operator-set actor_id."""
    plaintext = "a" * 32  # synthetic low-entropy fixture
    roles_path = _write_roles_toml_with_keys(tmp_path, [{
        "plaintext": plaintext,
        "actor_id": "service-EXAMPLE-1",
        "role": "contributor",
        "quota": 2.0,
        "slug": "svc1",
    }])
    monkeypatch.setenv("TFGEN_ROLES_TOML", roles_path)
    import importlib
    import api._auth as auth
    importlib.reload(auth)
    entry = auth.resolve_token(plaintext)
    assert entry is not None
    assert entry.actor_id == "service-EXAMPLE-1"
    assert entry.role == "contributor"
    assert abs(entry.daily_quota_usd - 2.0) < 1e-6


def test_per_key_token_wrong_token_returns_none(tmp_path, monkeypatch):
    plaintext = "b" * 32
    roles_path = _write_roles_toml_with_keys(tmp_path, [{
        "plaintext": plaintext,
        "actor_id": "svc",
        "role": "viewer",
        "quota": 0.5,
    }])
    monkeypatch.setenv("TFGEN_ROLES_TOML", roles_path)
    import importlib
    import api._auth as auth
    importlib.reload(auth)
    assert auth.resolve_token("c" * 32) is None


def test_legacy_tfgen_api_key_still_works(tmp_path, monkeypatch):
    """Backwards compat: TFGEN_API_KEY env var resolves to `legacy-tfgen` actor."""
    # Empty roles.toml -> only the env var should match.
    p = tmp_path / "roles.toml"
    p.write_text("[cost]\ndaily_cap_usd = 5.00\n", encoding="utf-8")
    monkeypatch.setenv("TFGEN_ROLES_TOML", str(p))
    monkeypatch.setenv("TFGEN_API_KEY", "legacy-key-EXAMPLE")
    import importlib
    import api._auth as auth
    importlib.reload(auth)
    # Per-key lookup must NOT match TFGEN_API_KEY.
    assert auth.resolve_token("legacy-key-EXAMPLE") is None
    # quota_for_actor(legacy-tfgen) -> [cost] daily_cap_usd fallback.
    monkeypatch.delenv("TFGEN_HTTP_DAILY_QUOTA_USD", raising=False)
    assert auth.quota_for_actor("legacy-tfgen") == 5.00


def test_verify_api_key_per_key_path(tmp_path, monkeypatch):
    """End-to-end: a per-key token in the X-API-Key header returns the actor_id."""
    plaintext = "d" * 32
    roles_path = _write_roles_toml_with_keys(tmp_path, [{
        "plaintext": plaintext,
        "actor_id": "actor-PER-KEY",
        "role": "editor",
        "quota": 10.0,
    }])
    monkeypatch.setenv("TFGEN_ROLES_TOML", roles_path)
    import importlib
    import api._auth as auth
    importlib.reload(auth)
    import asyncio
    # Per-key token in X-API-Key header
    got = asyncio.run(auth.verify_api_key(x_api_key=plaintext, authorization=None))
    assert got == "actor-PER-KEY"


def test_verify_api_key_bearer_header(tmp_path, monkeypatch):
    """A per-key token in Authorization: Bearer is also accepted."""
    plaintext = "e" * 32
    roles_path = _write_roles_toml_with_keys(tmp_path, [{
        "plaintext": plaintext,
        "actor_id": "actor-BEARER",
        "role": "viewer",
        "quota": 0.5,
    }])
    monkeypatch.setenv("TFGEN_ROLES_TOML", roles_path)
    import importlib
    import api._auth as auth
    importlib.reload(auth)
    import asyncio
    got = asyncio.run(auth.verify_api_key(x_api_key=None, authorization=f"Bearer {plaintext}"))
    assert got == "actor-BEARER"


def test_verify_api_key_rejects_unknown_token(tmp_path, monkeypatch):
    """A token absent from both [api_keys] and TFGEN_API_KEY env returns 401."""
    p = tmp_path / "roles.toml"
    p.write_text("[cost]\ndaily_cap_usd = 5.00\n", encoding="utf-8")
    monkeypatch.setenv("TFGEN_ROLES_TOML", str(p))
    monkeypatch.delenv("TFGEN_API_KEY", raising=False)
    import importlib
    import api._auth as auth
    importlib.reload(auth)
    import asyncio
    from fastapi import HTTPException
    try:
        asyncio.run(auth.verify_api_key(x_api_key="not-a-real-key", authorization=None))
    except HTTPException as e:
        assert e.status_code == 401
        return
    raise AssertionError("expected 401 HTTPException")


def test_verify_api_key_missing_returns_401(tmp_path, monkeypatch):
    p = tmp_path / "roles.toml"
    p.write_text("[cost]\ndaily_cap_usd = 5.00\n", encoding="utf-8")
    monkeypatch.setenv("TFGEN_ROLES_TOML", str(p))
    monkeypatch.delenv("TFGEN_API_KEY", raising=False)
    import importlib
    import api._auth as auth
    importlib.reload(auth)
    import asyncio
    from fastapi import HTTPException
    try:
        asyncio.run(auth.verify_api_key(x_api_key=None, authorization=None))
    except HTTPException as e:
        assert e.status_code == 401
        return
    raise AssertionError("expected 401 HTTPException")


def test_quota_for_actor_per_key(tmp_path, monkeypatch):
    """quota_for_actor returns the [api_keys] entry's daily_quota_usd."""
    plaintext = "f" * 32
    roles_path = _write_roles_toml_with_keys(tmp_path, [{
        "plaintext": plaintext,
        "actor_id": "qfa-actor",
        "role": "contributor",
        "quota": 3.50,
    }])
    monkeypatch.setenv("TFGEN_ROLES_TOML", roles_path)
    import importlib
    import api._auth as auth
    importlib.reload(auth)
    assert auth.quota_for_actor("qfa-actor") == 3.50
    assert auth.quota_for_actor("nonexistent") == 0.0


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
