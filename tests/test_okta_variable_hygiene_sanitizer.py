"""Tests for `generator.okta_variable_hygiene_sanitizer`.

Standalone-runnable: `python tests/test_okta_variable_hygiene_sanitizer.py`.
"""

from __future__ import annotations

import os
import re
import sys
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from generator.okta_variable_hygiene_sanitizer import sanitize_okta_variable_hygiene


def _wrap(hcl: str) -> dict:
    return {"terraform_okta_hcl": hcl}


def _declared_vars(hcl: str) -> set[str]:
    return set(re.findall(r'variable\s+"([a-zA-Z_][a-zA-Z0-9_]*)"', hcl))


def test_auth02_missing_variables_are_declared():
    """AUTH02: var.auth_server_name and var.auth_server_description are referenced
    but not declared. After sanitizing, both must have a `variable {}` block."""
    hcl = textwrap.dedent('''\
        resource "okta_auth_server" "mobile_api" {
          name        = var.auth_server_name
          description = var.auth_server_description
          audiences   = ["api://mobile"]
        }

        variable "okta_api_token" {
          type      = string
          sensitive = true
        }
        ''')
    out = sanitize_okta_variable_hygiene(_wrap(hcl))["terraform_okta_hcl"]
    declared = _declared_vars(out)
    assert "auth_server_name" in declared
    assert "auth_server_description" in declared
    assert "okta_api_token" in declared  # unchanged


def test_all_vars_declared_is_noop():
    hcl = textwrap.dedent('''\
        resource "okta_group" "engineering" {
          name = var.group_name
        }

        variable "group_name" {
          type    = string
          default = "Engineering"
        }
        ''')
    out = sanitize_okta_variable_hygiene(_wrap(hcl))["terraform_okta_hcl"]
    assert out == hcl


def test_no_var_references_is_noop():
    hcl = 'resource "okta_group" "g" {\n  name = "G"\n}\n'
    out = sanitize_okta_variable_hygiene(_wrap(hcl))["terraform_okta_hcl"]
    assert out == hcl


def test_idempotent():
    hcl = 'resource "okta_group" "g" {\n  name = var.group_name\n}\n'
    once = sanitize_okta_variable_hygiene(_wrap(hcl))
    twice = sanitize_okta_variable_hygiene(once)
    assert once["terraform_okta_hcl"] == twice["terraform_okta_hcl"]


def test_empty_input_is_noop():
    assert sanitize_okta_variable_hygiene({"terraform_okta_hcl": ""}) == {"terraform_okta_hcl": ""}
    assert sanitize_okta_variable_hygiene({}) == {}


def test_em01_strips_interpolation_from_default():
    """EM01/ED05/COMP08 shape: variable default contains ${...} interpolation.
    Terraform rejects this at init. Sanitizer must strip the interpolation,
    leaving the literal surrounding text."""
    hcl = textwrap.dedent('''\
        variable "user_activation_subject" {
          type        = string
          description = "Subject line"
          default     = "Welcome to ${org.name}"
        }
        ''')
    out = sanitize_okta_variable_hygiene(_wrap(hcl))["terraform_okta_hcl"]
    assert "${org.name}" not in out
    assert '"Welcome to "' in out, f"literal text must survive scrub; got: {out!r}"


def test_default_without_interpolation_unchanged():
    """Defaults without ${...} are left exactly as-is."""
    hcl = textwrap.dedent('''\
        variable "foo" {
          type    = string
          default = "Welcome to Okta"
        }
        ''')
    out = sanitize_okta_variable_hygiene(_wrap(hcl))["terraform_okta_hcl"]
    assert out == hcl


def test_interpolation_in_resource_block_untouched():
    """Interpolations OUTSIDE variable blocks (e.g. inside resource bodies)
    must not be scrubbed — they are legal HCL."""
    hcl = textwrap.dedent('''\
        resource "okta_app_oauth" "x" {
          label = "App for ${var.tenant}"
        }
        ''')
    out = sanitize_okta_variable_hygiene(_wrap(hcl))["terraform_okta_hcl"]
    assert "${var.tenant}" in out


def test_multiple_missing_vars_are_alphabetised():
    """Stub declarations should be deterministic ordering."""
    hcl = textwrap.dedent('''\
        resource "okta_app_oauth" "x" {
          label = var.zeta_label
          type  = var.alpha_type
        }
        ''')
    out = sanitize_okta_variable_hygiene(_wrap(hcl))["terraform_okta_hcl"]
    alpha_pos = out.index('variable "alpha_type"')
    zeta_pos = out.index('variable "zeta_label"')
    assert alpha_pos < zeta_pos, "alphabetical ordering expected for stub declarations"


if __name__ == "__main__":
    import traceback
    failures = []
    tests = [
        test_auth02_missing_variables_are_declared,
        test_all_vars_declared_is_noop,
        test_no_var_references_is_noop,
        test_idempotent,
        test_empty_input_is_noop,
        test_em01_strips_interpolation_from_default,
        test_default_without_interpolation_unchanged,
        test_interpolation_in_resource_block_untouched,
        test_multiple_missing_vars_are_alphabetised,
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
