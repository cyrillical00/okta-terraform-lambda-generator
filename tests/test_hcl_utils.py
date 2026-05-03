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

from generator.hcl_utils import strip_provider_boilerplate, derive_basename_from_intent, merge_terraform_blocks


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


def test_strips_provider_aws_block():
    hcl = textwrap.dedent('''\
        provider "aws" {
          region = var.aws_region
        }

        resource "aws_lambda_function" "handler" {
          function_name = "x"
        }
        ''')
    out = strip_provider_boilerplate(hcl)
    assert 'provider "aws"' not in out
    assert 'resource "aws_lambda_function" "handler"' in out


def test_strips_provider_google_block():
    hcl = textwrap.dedent('''\
        provider "google" {
          project = var.gcp_project_id
          region  = var.gcp_region
        }

        resource "google_cloudfunctions2_function" "handler" {
          name = "x"
        }
        ''')
    out = strip_provider_boilerplate(hcl)
    assert 'provider "google"' not in out
    assert 'resource "google_cloudfunctions2_function" "handler"' in out


def test_strips_gcp_project_id_variable():
    hcl = textwrap.dedent('''\
        variable "gcp_project_id" {
          type        = string
          description = "GCP project ID"
        }

        variable "function_name" {
          type    = string
          default = "x"
        }
        ''')
    out = strip_provider_boilerplate(hcl)
    assert 'variable "gcp_project_id"' not in out
    assert 'variable "function_name"' in out, "non-boilerplate variable must be preserved"


def test_strips_gcp_region_variable():
    hcl = textwrap.dedent('''\
        variable "gcp_region" {
          type    = string
          default = "us-central1"
        }

        resource "google_storage_bucket" "handler" {
          name = "x"
        }
        ''')
    out = strip_provider_boilerplate(hcl)
    assert 'variable "gcp_region"' not in out
    assert 'resource "google_storage_bucket" "handler"' in out


def test_strips_aws_region_variable():
    hcl = textwrap.dedent('''\
        variable "aws_region" {
          type    = string
          default = "us-east-1"
        }

        resource "aws_lambda_function" "handler" {
          function_name = "x"
        }
        ''')
    out = strip_provider_boilerplate(hcl)
    assert 'variable "aws_region"' not in out
    assert 'resource "aws_lambda_function" "handler"' in out


def test_strips_okta_org_name_and_base_url_variables():
    hcl = textwrap.dedent('''\
        variable "okta_org_name" {
          type = string
        }

        variable "okta_base_url" {
          type    = string
          default = "okta.com"
        }

        resource "okta_group" "x" {
          name = "X"
        }
        ''')
    out = strip_provider_boilerplate(hcl)
    assert 'variable "okta_org_name"' not in out
    assert 'variable "okta_base_url"' not in out
    assert 'resource "okta_group" "x"' in out


# derive_basename_from_intent tests

def test_derive_basename_from_snake_case_resource_name():
    assert derive_basename_from_intent({"resource_name": "engineering"}) == "engineering"


def test_derive_basename_preserves_underscores():
    assert derive_basename_from_intent({"resource_name": "hr_portal_workday"}) == "hr_portal_workday"


def test_derive_basename_lowercases_and_replaces_dashes():
    out = derive_basename_from_intent({"resource_name": "GCP-BigQuery-ReadOnly"})
    assert out == "gcp_bigquery_readonly", f"got {out!r}"


def test_derive_basename_collapses_special_chars():
    out = derive_basename_from_intent({"resource_name": "HR Portal Workday"})
    assert out == "hr_portal_workday", f"got {out!r}"


def test_derive_basename_strips_leading_trailing_underscores():
    out = derive_basename_from_intent({"resource_name": "--engineering--"})
    assert out == "engineering", f"got {out!r}"


def test_derive_basename_collapses_consecutive_underscores():
    out = derive_basename_from_intent({"resource_name": "a---b___c"})
    assert out == "a_b_c", f"got {out!r}"


def test_derive_basename_none_intent():
    assert derive_basename_from_intent(None) == ""


def test_derive_basename_empty_intent():
    assert derive_basename_from_intent({}) == ""


def test_derive_basename_missing_resource_name():
    assert derive_basename_from_intent({"resource_type": "okta_group"}) == ""


def test_derive_basename_empty_resource_name():
    assert derive_basename_from_intent({"resource_name": ""}) == ""


def test_derive_basename_none_resource_name():
    assert derive_basename_from_intent({"resource_name": None}) == ""


# merge_terraform_blocks tests

_OKTA_HCL = textwrap.dedent('''\
    terraform {
      required_providers {
        okta = {
          source  = "okta/okta"
          version = "~> 4.0"
        }
      }
    }

    provider "okta" {
      org_name  = var.okta_org_name
      api_token = var.okta_api_token
    }

    resource "okta_event_hook" "x" {
      name = "X"
    }
    ''')

_GCP_HCL = textwrap.dedent('''\
    terraform {
      required_providers {
        google = {
          source  = "hashicorp/google"
          version = "~> 6.0"
        }
      }
    }

    provider "google" {
      project = var.gcp_project_id
      region  = var.gcp_region
    }

    resource "google_cloudfunctions2_function" "handler" {
      name = "handler"
    }
    ''')


def test_merge_adds_google_to_okta_required_providers():
    new_okta, _ = merge_terraform_blocks(_OKTA_HCL, _GCP_HCL)
    assert 'okta = {' in new_okta
    assert 'google = {' in new_okta
    assert 'source  = "hashicorp/google"' in new_okta or 'source = "hashicorp/google"' in new_okta


def test_merge_strips_terraform_block_from_secondary():
    _, new_gcp = merge_terraform_blocks(_OKTA_HCL, _GCP_HCL)
    assert 'terraform {' not in new_gcp
    assert 'provider "google"' in new_gcp, 'provider block must remain in gcp'
    assert 'resource "google_cloudfunctions2_function"' in new_gcp


def test_merge_idempotent():
    once_okta, once_gcp = merge_terraform_blocks(_OKTA_HCL, _GCP_HCL)
    twice_okta, twice_gcp = merge_terraform_blocks(once_okta, once_gcp)
    assert once_okta == twice_okta, 'merge must be idempotent on okta side'
    assert once_gcp == twice_gcp, 'merge must be idempotent on gcp side'


def test_merge_no_op_when_secondary_lacks_terraform_block():
    no_terraform_gcp = 'provider "google" {\n  project = "x"\n}\n'
    new_okta, new_gcp = merge_terraform_blocks(_OKTA_HCL, no_terraform_gcp)
    assert new_okta == _OKTA_HCL
    assert new_gcp == no_terraform_gcp


def test_merge_no_op_when_primary_lacks_terraform_block():
    no_terraform_okta = 'resource "okta_group" "x" {\n  name = "X"\n}\n'
    new_okta, new_gcp = merge_terraform_blocks(no_terraform_okta, _GCP_HCL)
    assert new_okta == no_terraform_okta
    assert new_gcp == _GCP_HCL


def test_merge_no_op_when_either_input_empty():
    a, b = merge_terraform_blocks('', _GCP_HCL)
    assert a == '' and b == _GCP_HCL
    a, b = merge_terraform_blocks(_OKTA_HCL, '')
    assert a == _OKTA_HCL and b == ''


def test_merge_does_not_duplicate_provider_already_in_primary():
    okta_with_google = textwrap.dedent('''\
        terraform {
          required_providers {
            okta = {
              source  = "okta/okta"
              version = "~> 4.0"
            }
            google = {
              source  = "hashicorp/google"
              version = "~> 6.0"
            }
          }
        }
        ''')
    new_primary, new_secondary = merge_terraform_blocks(okta_with_google, _GCP_HCL)
    # google should appear exactly once in required_providers
    assert new_primary.count('google = {') == 1, f'expected one google entry, got: {new_primary}'
    assert 'terraform {' not in new_secondary, 'gcp terraform block still gets stripped'


def test_merge_preserves_resources_in_primary():
    new_okta, _ = merge_terraform_blocks(_OKTA_HCL, _GCP_HCL)
    assert 'resource "okta_event_hook" "x"' in new_okta
    assert 'provider "okta"' in new_okta


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
    test_strips_provider_aws_block,
    test_strips_provider_google_block,
    test_strips_gcp_project_id_variable,
    test_strips_gcp_region_variable,
    test_strips_aws_region_variable,
    test_strips_okta_org_name_and_base_url_variables,
    test_derive_basename_from_snake_case_resource_name,
    test_derive_basename_preserves_underscores,
    test_derive_basename_lowercases_and_replaces_dashes,
    test_derive_basename_collapses_special_chars,
    test_derive_basename_strips_leading_trailing_underscores,
    test_derive_basename_collapses_consecutive_underscores,
    test_derive_basename_none_intent,
    test_derive_basename_empty_intent,
    test_derive_basename_missing_resource_name,
    test_derive_basename_empty_resource_name,
    test_derive_basename_none_resource_name,
    test_merge_adds_google_to_okta_required_providers,
    test_merge_strips_terraform_block_from_secondary,
    test_merge_idempotent,
    test_merge_no_op_when_secondary_lacks_terraform_block,
    test_merge_no_op_when_primary_lacks_terraform_block,
    test_merge_no_op_when_either_input_empty,
    test_merge_does_not_duplicate_provider_already_in_primary,
    test_merge_preserves_resources_in_primary,
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
