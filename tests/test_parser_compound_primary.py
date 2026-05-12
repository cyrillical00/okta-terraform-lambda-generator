"""Tests for `generator.parser._normalize_compound_primary`.

Standalone-runnable: `python tests/test_parser_compound_primary.py`.
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from generator.parser import _normalize_compound_primary


def test_comp01_promotes_oauth_app_to_primary():
    """COMP01 shape: LLM routed primary to okta_auth_server_scope but the
    resource_types list correctly contains okta_app_oauth + okta_auth_server.
    Helper must promote okta_app_oauth to primary."""
    intent = {
        "resource_type": "okta_auth_server_scope",
        "resource_types": ["okta_app_oauth", "okta_auth_server", "okta_auth_server_scope"],
    }
    out = _normalize_compound_primary(intent)
    assert out["resource_type"] == "okta_app_oauth"
    # resource_types unchanged.
    assert out["resource_types"] == ["okta_app_oauth", "okta_auth_server", "okta_auth_server_scope"]


def test_saml_app_plus_scope_promotes_saml():
    """Compound SAML + scope (not in current test corpus but plausible)."""
    intent = {
        "resource_type": "okta_auth_server_scope",
        "resource_types": ["okta_app_saml", "okta_auth_server_scope"],
    }
    out = _normalize_compound_primary(intent)
    assert out["resource_type"] == "okta_app_saml"


def test_oauth_wins_over_saml_when_both_present():
    """Edge case: if both app types appear, oauth is the deterministic winner."""
    intent = {
        "resource_type": "okta_auth_server_claim",
        "resource_types": ["okta_app_oauth", "okta_app_saml", "okta_auth_server_claim"],
    }
    out = _normalize_compound_primary(intent)
    assert out["resource_type"] == "okta_app_oauth"


def test_plain_scope_request_unchanged():
    """A plain 'Add a scope to <server>' prompt has only the child in
    resource_types. No app to promote. Primary stays as the child."""
    intent = {
        "resource_type": "okta_auth_server_scope",
        "resource_types": ["okta_auth_server_scope"],
    }
    out = _normalize_compound_primary(intent)
    assert out["resource_type"] == "okta_auth_server_scope"


def test_already_app_primary_unchanged():
    """If the LLM already returned the app as primary, the post-process is a
    no-op."""
    intent = {
        "resource_type": "okta_app_oauth",
        "resource_types": ["okta_app_oauth", "okta_auth_server_scope"],
    }
    out = _normalize_compound_primary(intent)
    assert out["resource_type"] == "okta_app_oauth"


def test_unrelated_intent_unchanged():
    """Non-auth-server intents pass through untouched."""
    intent = {
        "resource_type": "okta_group",
        "resource_types": ["okta_group"],
    }
    out = _normalize_compound_primary(intent)
    assert out["resource_type"] == "okta_group"


def test_idempotent():
    intent = {
        "resource_type": "okta_auth_server_scope",
        "resource_types": ["okta_app_oauth", "okta_auth_server_scope"],
    }
    once = _normalize_compound_primary(dict(intent))
    twice = _normalize_compound_primary(once)
    assert once["resource_type"] == twice["resource_type"] == "okta_app_oauth"


def test_missing_resource_types_unchanged():
    """If resource_types is missing or empty, the helper is a no-op."""
    intent = {"resource_type": "okta_auth_server_scope"}
    out = _normalize_compound_primary(intent)
    assert out["resource_type"] == "okta_auth_server_scope"


def test_policy_child_promotes_app():
    """okta_auth_server_policy_rule + okta_app_oauth should also promote
    the app (the compound rule applies to all four auth-server child types)."""
    intent = {
        "resource_type": "okta_auth_server_policy_rule",
        "resource_types": ["okta_app_oauth", "okta_auth_server_policy_rule"],
    }
    out = _normalize_compound_primary(intent)
    assert out["resource_type"] == "okta_app_oauth"


if __name__ == "__main__":
    import traceback
    failures = []
    tests = [
        test_comp01_promotes_oauth_app_to_primary,
        test_saml_app_plus_scope_promotes_saml,
        test_oauth_wins_over_saml_when_both_present,
        test_plain_scope_request_unchanged,
        test_already_app_primary_unchanged,
        test_unrelated_intent_unchanged,
        test_idempotent,
        test_missing_resource_types_unchanged,
        test_policy_child_promotes_app,
    ]
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception:
            failures.append(t.__name__)
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
