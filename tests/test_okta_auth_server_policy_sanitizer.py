"""Tests for `generator.okta_auth_server_policy_sanitizer`.

Standalone-runnable:
    python tests/test_okta_auth_server_policy_sanitizer.py
"""

from __future__ import annotations

import os
import sys
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from generator.okta_auth_server_policy_sanitizer import (
    sanitize_okta_auth_server_policy,
)


# ── Positive drift cases ───────────────────────────────────────────────────


def test_clients_inline_list_rewritten():
    """AUTH05 canonical drift: `clients = ["ALL_CLIENTS"]` ->
    `client_whitelist = ["ALL_CLIENTS"]`."""
    hcl = textwrap.dedent('''\
        resource "okta_auth_server_policy" "payments_policy" {
          auth_server_id = var.auth_server_id
          name           = "Payments Policy"
          status         = "ACTIVE"
          description    = "Default access policy"
          priority       = 1
          clients        = ["ALL_CLIENTS"]
        }
        ''')
    result = sanitize_okta_auth_server_policy({"terraform_okta_hcl": hcl})
    out = result["terraform_okta_hcl"]
    # The original `clients =` attribute line is gone.
    for line in out.splitlines():
        stripped = line.lstrip()
        assert not stripped.startswith("clients ="), f"leaked: {line!r}"
        assert not stripped.startswith("clients  ="), f"leaked: {line!r}"
        assert not stripped.startswith("clients   ="), f"leaked: {line!r}"
    assert 'client_whitelist' in out
    assert '["ALL_CLIENTS"]' in out


def test_clients_with_explicit_client_ids():
    """Drift with explicit client ID list."""
    hcl = textwrap.dedent('''\
        resource "okta_auth_server_policy" "restricted" {
          auth_server_id = var.auth_server_id
          name           = "Restricted"
          priority       = 2
          clients        = ["0oaclient1", "0oaclient2"]
        }
        ''')
    result = sanitize_okta_auth_server_policy({"terraform_okta_hcl": hcl})
    out = result["terraform_okta_hcl"]
    assert 'client_whitelist' in out
    assert '"0oaclient1"' in out
    assert '"0oaclient2"' in out


def test_multiple_policies_all_rewritten():
    hcl = textwrap.dedent('''\
        resource "okta_auth_server_policy" "policy_a" {
          name    = "A"
          clients = ["ALL_CLIENTS"]
        }
        resource "okta_auth_server_policy" "policy_b" {
          name    = "B"
          clients = ["specific_client"]
        }
        ''')
    result = sanitize_okta_auth_server_policy({"terraform_okta_hcl": hcl})
    out = result["terraform_okta_hcl"]
    # Both blocks should have `client_whitelist` now.
    assert out.count("client_whitelist") == 2
    assert out.count("\n  clients ") == 0


# ── Negative / idempotent cases ────────────────────────────────────────────


def test_clean_hcl_unchanged():
    """A policy that already uses `client_whitelist` is left alone."""
    hcl = textwrap.dedent('''\
        resource "okta_auth_server_policy" "clean" {
          name             = "Clean"
          client_whitelist = ["ALL_CLIENTS"]
        }
        ''')
    result = sanitize_okta_auth_server_policy({"terraform_okta_hcl": hcl})
    assert result["terraform_okta_hcl"] == hcl


def test_idempotent():
    hcl = textwrap.dedent('''\
        resource "okta_auth_server_policy" "x" {
          name    = "X"
          clients = ["ALL_CLIENTS"]
        }
        ''')
    once = sanitize_okta_auth_server_policy({"terraform_okta_hcl": hcl})
    twice = sanitize_okta_auth_server_policy(once)
    assert once["terraform_okta_hcl"] == twice["terraform_okta_hcl"]


def test_no_policy_block_is_noop():
    hcl = 'resource "okta_group" "engineering" {\n  name = "Engineering"\n}\n'
    result = sanitize_okta_auth_server_policy({"terraform_okta_hcl": hcl})
    assert result["terraform_okta_hcl"] == hcl


def test_child_policy_rule_not_touched():
    """The okta_auth_server_policy_rule child resource has its own attribute
    schema; this sanitizer must not touch it. Its body might have other
    attributes like `clients` in a different context, though in practice it
    does not. The test ensures the rule-block opener is not matched by the
    parent-policy regex."""
    hcl = textwrap.dedent('''\
        resource "okta_auth_server_policy_rule" "rule_a" {
          auth_server_id = var.auth_server_id
          policy_id      = var.policy_id
          name           = "Rule A"
          priority       = 1
        }
        ''')
    result = sanitize_okta_auth_server_policy({"terraform_okta_hcl": hcl})
    # Identical output: the rule resource is out of scope.
    assert result["terraform_okta_hcl"] == hcl


def test_input_dict_not_mutated():
    hcl = textwrap.dedent('''\
        resource "okta_auth_server_policy" "x" {
          name    = "X"
          clients = ["ALL_CLIENTS"]
        }
        ''')
    outputs = {"terraform_okta_hcl": hcl}
    sanitize_okta_auth_server_policy(outputs)
    assert outputs["terraform_okta_hcl"] == hcl


if __name__ == "__main__":
    import traceback
    failures = []
    tests = [
        test_clients_inline_list_rewritten,
        test_clients_with_explicit_client_ids,
        test_multiple_policies_all_rewritten,
        test_clean_hcl_unchanged,
        test_idempotent,
        test_no_policy_block_is_noop,
        test_child_policy_rule_not_touched,
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
