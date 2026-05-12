"""Tests for `generator.okta_data_source_sanitizer`.

Standalone-runnable: `python tests/test_okta_data_source_sanitizer.py`.
"""

from __future__ import annotations

import os
import sys
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from generator.okta_data_source_sanitizer import sanitize_okta_data_source_refs


def _wrap(hcl: str) -> dict:
    return {"terraform_okta_hcl": hcl}


def test_comp04_strips_hallucinated_default_policy_block():
    """COMP04: data 'okta_auth_server_default_policy' does not exist in v4.x.
    Sanitizer must remove the block."""
    hcl = textwrap.dedent('''\
        data "okta_auth_server" "default" {
          name = "default"
        }

        data "okta_auth_server_default_policy" "default" {
          auth_server_id = data.okta_auth_server.default.id
        }

        resource "okta_auth_server_policy_rule" "rule" {
          policy_id = data.okta_auth_server_default_policy.default.id
        }
        ''')
    out = sanitize_okta_data_source_refs(_wrap(hcl))["terraform_okta_hcl"]
    assert "okta_auth_server_default_policy" not in out, \
        f"hallucinated type must be scrubbed from data block AND references; got: {out!r}"
    # Valid data source untouched.
    assert 'data "okta_auth_server" "default"' in out
    # Reference rewritten to var.X placeholder.
    assert "var.auth_server_default_policy_default_id" in out
    # The dependent resource block is preserved (just the reference is rewritten).
    assert 'resource "okta_auth_server_policy_rule" "rule"' in out


def test_no_hallucinated_types_is_noop():
    hcl = textwrap.dedent('''\
        data "okta_auth_server" "default" {
          name = "default"
        }

        data "okta_group" "engineering" {
          name = "Engineering"
        }
        ''')
    out = sanitize_okta_data_source_refs(_wrap(hcl))["terraform_okta_hcl"]
    assert out == hcl


def test_empty_input_is_noop():
    assert sanitize_okta_data_source_refs({"terraform_okta_hcl": ""}) == {"terraform_okta_hcl": ""}
    assert sanitize_okta_data_source_refs({}) == {}


def test_idempotent():
    hcl = textwrap.dedent('''\
        data "okta_auth_server_default_policy" "default" {
          auth_server_id = "abc"
        }

        output "x" {
          value = data.okta_auth_server_default_policy.default.id
        }
        ''')
    once = sanitize_okta_data_source_refs(_wrap(hcl))
    twice = sanitize_okta_data_source_refs(once)
    assert once["terraform_okta_hcl"] == twice["terraform_okta_hcl"]


def test_input_dict_not_mutated():
    hcl = textwrap.dedent('''\
        data "okta_auth_server_default_policy" "x" {
          auth_server_id = "y"
        }
        ''')
    outputs = _wrap(hcl)
    original = outputs["terraform_okta_hcl"]
    sanitize_okta_data_source_refs(outputs)
    assert outputs["terraform_okta_hcl"] == original


if __name__ == "__main__":
    import traceback
    failures = []
    tests = [
        test_comp04_strips_hallucinated_default_policy_block,
        test_no_hallucinated_types_is_noop,
        test_empty_input_is_noop,
        test_idempotent,
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
