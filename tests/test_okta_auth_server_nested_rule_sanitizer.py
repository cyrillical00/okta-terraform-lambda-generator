"""Tests for the Phase 20.1 nested-resource-hoist sanitizer (AUTH05).

Verifies that `resource "okta_auth_server_policy_rule"` blocks nested
inside `resource "okta_auth_server_policy"` blocks are hoisted to
top-level, preserving the policy_id linkage that the LLM already wrote.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generator.okta_auth_server_nested_rule_sanitizer import (
    sanitize_okta_auth_server_nested_rule,
)


def _wrap(hcl: str) -> dict:
    return {"terraform_okta_hcl": hcl}


def test_hoist_single_nested_rule():
    """The canonical AUTH05 drift: one nested rule inside one policy."""
    hcl = """resource "okta_auth_server_policy" "p" {
  auth_server_id   = var.auth_server_id
  name             = "One Hour Lifetime"
  status           = "ACTIVE"
  priority         = 1
  client_whitelist = ["ALL_CLIENTS"]

  resource "okta_auth_server_policy_rule" "r" {
    auth_server_id                = var.auth_server_id
    policy_id                     = okta_auth_server_policy.p.id
    name                          = "Rule"
    status                        = "ACTIVE"
    priority                      = 1
    grant_type_whitelist          = ["authorization_code"]
    scope_whitelist               = ["*"]
    group_whitelist               = ["EVERYONE"]
    access_token_lifetime_minutes = 60
  }
}
"""
    out = sanitize_okta_auth_server_nested_rule(_wrap(hcl))
    result = out["terraform_okta_hcl"]
    # The nested rule should be gone from the parent body
    assert result.count('resource "okta_auth_server_policy_rule"') == 1
    # And present at top level: zero leading spaces before its opener
    assert '\nresource "okta_auth_server_policy_rule" "r" {' in result
    # The policy_id reference is preserved verbatim
    assert "policy_id                     = okta_auth_server_policy.p.id" in result
    # The parent policy still closes cleanly with its attrs intact
    assert "client_whitelist = " in result
    # No nested-resource depth left
    parent_start = result.index('resource "okta_auth_server_policy" "p"')
    rule_start = result.index('resource "okta_auth_server_policy_rule" "r"')
    parent_close = result.index("\n}", parent_start)
    assert parent_close < rule_start, "parent must close before the rule begins"


def test_idempotent_on_clean_hcl():
    """Already-clean HCL (siblings linked via policy_id) is unchanged."""
    hcl = """resource "okta_auth_server_policy" "p" {
  auth_server_id   = var.auth_server_id
  name             = "Policy"
  status           = "ACTIVE"
  priority         = 1
  client_whitelist = ["ALL_CLIENTS"]
}

resource "okta_auth_server_policy_rule" "r" {
  auth_server_id = var.auth_server_id
  policy_id      = okta_auth_server_policy.p.id
  name           = "Rule"
  status         = "ACTIVE"
  priority       = 1
  grant_type_whitelist = ["authorization_code"]
  scope_whitelist      = ["*"]
  group_whitelist      = ["EVERYONE"]
}
"""
    once = sanitize_okta_auth_server_nested_rule(_wrap(hcl))["terraform_okta_hcl"]
    twice = sanitize_okta_auth_server_nested_rule(_wrap(once))["terraform_okta_hcl"]
    assert once == hcl, "clean HCL should be returned unchanged"
    assert once == twice, "sanitizer must be idempotent"


def test_idempotent_after_hoist():
    """Running the sanitizer twice on drifted HCL produces the same result
    as running it once. (Hoist, then verify hoist is a fixed point.)"""
    drifted = """resource "okta_auth_server_policy" "p" {
  name = "P"

  resource "okta_auth_server_policy_rule" "r" {
    policy_id = okta_auth_server_policy.p.id
    name      = "R"
  }
}
"""
    once = sanitize_okta_auth_server_nested_rule(_wrap(drifted))["terraform_okta_hcl"]
    twice = sanitize_okta_auth_server_nested_rule(_wrap(once))["terraform_okta_hcl"]
    assert once == twice


def test_hoist_multiple_nested_rules_preserves_order():
    """Two nested rules in one parent come out as two top-level resources,
    in their original declaration order."""
    hcl = """resource "okta_auth_server_policy" "p" {
  name = "P"

  resource "okta_auth_server_policy_rule" "rule_a" {
    policy_id = okta_auth_server_policy.p.id
    name      = "A"
  }

  resource "okta_auth_server_policy_rule" "rule_b" {
    policy_id = okta_auth_server_policy.p.id
    name      = "B"
  }
}
"""
    result = sanitize_okta_auth_server_nested_rule(_wrap(hcl))["terraform_okta_hcl"]
    # Both rules at top level
    assert result.count('\nresource "okta_auth_server_policy_rule"') == 2
    # Order: rule_a before rule_b
    a_pos = result.index('"rule_a"')
    b_pos = result.index('"rule_b"')
    assert a_pos < b_pos, "extracted rules must keep original order"


def test_no_op_when_no_policy_present():
    """A file with only a rule (no parent policy) is left alone."""
    hcl = """resource "okta_auth_server_policy_rule" "r" {
  policy_id = "abc"
  name      = "R"
}
"""
    result = sanitize_okta_auth_server_nested_rule(_wrap(hcl))["terraform_okta_hcl"]
    assert result == hcl


def test_no_op_when_no_rule_present():
    """A file with only a policy (no rule at all) is left alone."""
    hcl = """resource "okta_auth_server_policy" "p" {
  name = "P"
}
"""
    result = sanitize_okta_auth_server_nested_rule(_wrap(hcl))["terraform_okta_hcl"]
    assert result == hcl


def test_unrelated_resources_untouched():
    """Other resource types in the same file are unaffected."""
    hcl = """resource "okta_group" "g" {
  name = "G"
}

resource "okta_auth_server_policy" "p" {
  name = "P"

  resource "okta_auth_server_policy_rule" "r" {
    policy_id = okta_auth_server_policy.p.id
    name      = "R"
  }
}

resource "okta_app_oauth" "a" {
  label = "A"
}
"""
    result = sanitize_okta_auth_server_nested_rule(_wrap(hcl))["terraform_okta_hcl"]
    # okta_group and okta_app_oauth untouched, in original positions
    assert 'resource "okta_group" "g" {\n  name = "G"\n}' in result
    assert 'resource "okta_app_oauth" "a" {\n  label = "A"\n}' in result
    # Rule hoisted to top level
    assert '\nresource "okta_auth_server_policy_rule" "r" {' in result


def test_braces_in_strings_dont_break_matching():
    """A description string containing `{` or `}` characters must not
    confuse the brace-matching parser."""
    hcl = '''resource "okta_auth_server_policy" "p" {
  name        = "P"
  description = "Rule with {curly} and }brace in description"

  resource "okta_auth_server_policy_rule" "r" {
    policy_id = okta_auth_server_policy.p.id
    name      = "R"
  }
}
'''
    result = sanitize_okta_auth_server_nested_rule(_wrap(hcl))["terraform_okta_hcl"]
    assert '\nresource "okta_auth_server_policy_rule" "r" {' in result
    # The string with curly braces must survive intact in the parent body
    assert '"Rule with {curly} and }brace in description"' in result


if __name__ == "__main__":
    import traceback
    failures = []
    for name in list(globals()):
        if name.startswith("test_"):
            fn = globals()[name]
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:
                failures.append((name, exc, traceback.format_exc()))
                print(f"FAIL {name}: {exc}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        for name, exc, tb in failures:
            print(f"\n--- {name} ---")
            print(tb)
        sys.exit(1)
    print(f"\nAll passed.")
