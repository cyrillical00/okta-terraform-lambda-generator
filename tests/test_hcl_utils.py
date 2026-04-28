"""Tests for `generator.hcl_utils`.

Standalone-runnable: `python tests/test_hcl_utils.py`.
"""

from __future__ import annotations

import os
import sys
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from generator.hcl_utils import strip_provider_boilerplate


CANONICAL_HCL_WITH_BOILERPLATE = textwrap.dedent('''\
    terraform {
      required_providers {
        okta = {
          source  = "okta/okta"
          version = "~> 4.0"
        }
      }
    }

    provider "okta" {
      org_name  = "integrator-2720791"
      base_url  = "okta.com"
      api_token = var.okta_api_token
    }

    variable "okta_api_token" {
      type        = string
      sensitive   = true
      description = "Okta API token"
    }

    variable "department_expression" {
      type        = string
      default     = "user.department == \\"Analytics\\""
    }

    resource "okta_group" "hr" {
      name        = "HR"
      description = "HR department"
    }
    ''')


def test_strips_terraform_block():
    out = strip_provider_boilerplate(CANONICAL_HCL_WITH_BOILERPLATE)
    assert 'terraform {' not in out, f"terraform block must be stripped, got: {out!r}"


def test_strips_provider_okta_block():
    out = strip_provider_boilerplate(CANONICAL_HCL_WITH_BOILERPLATE)
    assert 'provider "okta"' not in out, f"provider block must be stripped, got: {out!r}"


def test_strips_okta_api_token_variable():
    out = strip_provider_boilerplate(CANONICAL_HCL_WITH_BOILERPLATE)
    assert 'variable "okta_api_token"' not in out, \
        f"okta_api_token variable must be stripped, got: {out!r}"


def test_preserves_other_variables():
    out = strip_provider_boilerplate(CANONICAL_HCL_WITH_BOILERPLATE)
    assert 'variable "department_expression"' in out, \
        "non-boilerplate variables must be preserved"


def test_preserves_resource_blocks():
    out = strip_provider_boilerplate(CANONICAL_HCL_WITH_BOILERPLATE)
    assert 'resource "okta_group" "hr"' in out, \
        "resource blocks must be preserved"
    assert 'description = "HR department"' in out, \
        "resource attributes must be preserved"


def test_idempotent():
    once = strip_provider_boilerplate(CANONICAL_HCL_WITH_BOILERPLATE)
    twice = strip_provider_boilerplate(once)
    assert once == twice, "f(f(x)) must equal f(x) — idempotent contract"


def test_empty_string_noop():
    assert strip_provider_boilerplate("") == ""


def test_none_returns_none_passthrough():
    # Function accepts None? Spec says str. But guard for empty/None usage.
    out = strip_provider_boilerplate("")
    assert out == ""


def test_hcl_without_boilerplate_unchanged():
    hcl_no_boilerplate = textwrap.dedent('''\
        resource "okta_group" "engineering" {
          name = "Engineering"
        }
        ''')
    out = strip_provider_boilerplate(hcl_no_boilerplate)
    assert out == hcl_no_boilerplate, \
        "input without boilerplate must be returned unchanged (modulo leading newlines)"


def test_handles_nested_braces_in_terraform_block():
    """The terraform block contains required_providers with nested {}.
    The lazy regex must not stop at the inner closing braces."""
    hcl = textwrap.dedent('''\
        terraform {
          required_providers {
            okta = {
              source  = "okta/okta"
              version = "~> 4.0"
            }
          }
        }

        resource "okta_group" "x" {
          name = "X"
        }
        ''')
    out = strip_provider_boilerplate(hcl)
    assert 'terraform {' not in out
    assert 'required_providers' not in out
    assert 'resource "okta_group" "x"' in out


def test_provider_block_with_multiple_attributes():
    hcl = textwrap.dedent('''\
        provider "okta" {
          org_name  = "foo"
          base_url  = "okta.com"
          api_token = var.okta_api_token
        }

        resource "okta_group" "x" {
          name = "X"
        }
        ''')
    out = strip_provider_boilerplate(hcl)
    assert 'provider "okta"' not in out
    assert 'resource "okta_group" "x"' in out


_TESTS = [
    test_strips_terraform_block,
    test_strips_provider_okta_block,
    test_strips_okta_api_token_variable,
    test_preserves_other_variables,
    test_preserves_resource_blocks,
    test_idempotent,
    test_empty_string_noop,
    test_none_returns_none_passthrough,
    test_hcl_without_boilerplate_unchanged,
    test_handles_nested_braces_in_terraform_block,
    test_provider_block_with_multiple_attributes,
]


def main() -> int:
    passes = 0
    failures: list[tuple[str, str]] = []
    for fn in _TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
            passes += 1
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}: {e}")
            failures.append((fn.__name__, str(e)))
        except Exception as e:
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
            failures.append((fn.__name__, f"{type(e).__name__}: {e}"))

    print()
    print(f"{passes}/{len(_TESTS)} passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
