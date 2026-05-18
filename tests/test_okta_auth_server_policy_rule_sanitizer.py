"""Tests for `generator.okta_auth_server_policy_rule_sanitizer`.

Standalone-runnable:
    python tests/test_okta_auth_server_policy_rule_sanitizer.py
"""

from __future__ import annotations

import os
import sys
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from generator.okta_auth_server_policy_rule_sanitizer import (
    sanitize_okta_auth_server_policy_rule,
)


# ── Positive drift cases ───────────────────────────────────────────────────


def test_int_token_lifetime_is_rewritten():
    """AP02 canonical drift: `token_lifetime = 60` -> `access_token_lifetime_minutes = 60`."""
    hcl = textwrap.dedent('''\
        resource "okta_auth_server_policy_rule" "limit_token" {
          auth_server_id       = var.auth_server_id
          policy_id            = var.policy_id
          name                 = "Limit Token Lifetime"
          status               = "ACTIVE"
          priority             = 1
          grant_type_whitelist = ["authorization_code"]
          scope_whitelist      = ["*"]
          group_whitelist      = ["EVERYONE"]
          token_lifetime       = 60
        }
        ''')
    result = sanitize_okta_auth_server_policy_rule({"terraform_okta_hcl": hcl})
    out = result["terraform_okta_hcl"]
    # Bare `token_lifetime` attribute line is gone (the substring may still
    # appear inside `access_token_lifetime_minutes`).
    for line in out.splitlines():
        stripped = line.lstrip()
        assert not stripped.startswith("token_lifetime"), f"leaked: {line!r}"
    # New attribute is present with the original value (60).
    assert "access_token_lifetime_minutes" in out
    assert "= 60" in out


def test_var_ref_token_lifetime_is_rewritten():
    """Drift via var reference: `token_lifetime = var.token_lifetime_minutes`."""
    hcl = textwrap.dedent('''\
        resource "okta_auth_server_policy_rule" "limit_token" {
          auth_server_id = var.auth_server_id
          policy_id      = var.policy_id
          name           = "Limit Token Lifetime"
          token_lifetime = var.token_lifetime_minutes
        }
        ''')
    result = sanitize_okta_auth_server_policy_rule({"terraform_okta_hcl": hcl})
    out = result["terraform_okta_hcl"]
    assert "access_token_lifetime_minutes = var.token_lifetime_minutes" in out
    # Bare `token_lifetime =` must be gone.
    for line in out.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("token_lifetime ="), f"leaked: {line!r}"


def test_multiple_rules_all_rewritten():
    hcl = textwrap.dedent('''\
        resource "okta_auth_server_policy_rule" "rule_a" {
          name           = "A"
          token_lifetime = 30
        }
        resource "okta_auth_server_policy_rule" "rule_b" {
          name           = "B"
          token_lifetime = 120
        }
        ''')
    result = sanitize_okta_auth_server_policy_rule({"terraform_okta_hcl": hcl})
    out = result["terraform_okta_hcl"]
    assert "access_token_lifetime_minutes = 30" in out
    assert "access_token_lifetime_minutes = 120" in out
    assert "  token_lifetime " not in out


# ── Negative / idempotent cases ────────────────────────────────────────────


def test_clean_hcl_unchanged():
    """A rule that already uses `access_token_lifetime_minutes` is left alone."""
    hcl = textwrap.dedent('''\
        resource "okta_auth_server_policy_rule" "clean" {
          name                            = "Clean"
          access_token_lifetime_minutes   = 60
          refresh_token_lifetime_minutes  = 1440
        }
        ''')
    result = sanitize_okta_auth_server_policy_rule({"terraform_okta_hcl": hcl})
    assert result["terraform_okta_hcl"] == hcl


def test_idempotent():
    hcl = textwrap.dedent('''\
        resource "okta_auth_server_policy_rule" "x" {
          name           = "X"
          token_lifetime = 60
        }
        ''')
    once = sanitize_okta_auth_server_policy_rule({"terraform_okta_hcl": hcl})
    twice = sanitize_okta_auth_server_policy_rule(once)
    assert once["terraform_okta_hcl"] == twice["terraform_okta_hcl"]


def test_no_policy_rule_block_is_noop():
    hcl = 'resource "okta_group" "engineering" {\n  name = "Engineering"\n}\n'
    result = sanitize_okta_auth_server_policy_rule({"terraform_okta_hcl": hcl})
    assert result["terraform_okta_hcl"] == hcl


def test_out_of_scope_token_lifetime_left_alone():
    """A `token_lifetime` line outside any okta_auth_server_policy_rule block
    must NOT be rewritten. Use a variable block to demonstrate scope."""
    hcl = textwrap.dedent('''\
        variable "token_lifetime" {
          type    = number
          default = 60
        }

        resource "okta_group" "engineering" {
          name = "Engineering"
        }
        ''')
    result = sanitize_okta_auth_server_policy_rule({"terraform_okta_hcl": hcl})
    # No okta_auth_server_policy_rule block at all -> noop.
    assert result["terraform_okta_hcl"] == hcl


def test_input_dict_not_mutated():
    hcl = textwrap.dedent('''\
        resource "okta_auth_server_policy_rule" "x" {
          name           = "X"
          token_lifetime = 60
        }
        ''')
    outputs = {"terraform_okta_hcl": hcl}
    sanitize_okta_auth_server_policy_rule(outputs)
    assert outputs["terraform_okta_hcl"] == hcl


if __name__ == "__main__":
    import traceback
    failures = []
    tests = [
        test_int_token_lifetime_is_rewritten,
        test_var_ref_token_lifetime_is_rewritten,
        test_multiple_rules_all_rewritten,
        test_clean_hcl_unchanged,
        test_idempotent,
        test_no_policy_rule_block_is_noop,
        test_out_of_scope_token_lifetime_left_alone,
        test_input_dict_not_mutated,
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
