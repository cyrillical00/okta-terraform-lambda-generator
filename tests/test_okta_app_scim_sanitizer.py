"""Tests for `generator.okta_app_scim_sanitizer`.

Standalone-runnable: `python tests/test_okta_app_scim_sanitizer.py`.
"""

from __future__ import annotations

import os
import sys
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from generator.okta_app_scim_sanitizer import sanitize_okta_app_scim_refs


def _wrap(hcl: str) -> dict:
    return {"terraform_okta_hcl": hcl}


def test_strips_provisioning_block_on_saml_app():
    hcl = textwrap.dedent('''\
        resource "okta_app_saml" "workday" {
          label   = "Workday"
          sso_url = var.workday_sso_url

          provisioning {
            scim_url      = "https://example.com/scim"
            scim_enabled  = true
          }
        }
        ''')
    out = sanitize_okta_app_scim_refs(_wrap(hcl))["terraform_okta_hcl"]
    assert "provisioning {" not in out, f"provisioning block must be stripped: {out!r}"
    assert "scim_url" not in out
    assert "scim_enabled" not in out
    assert 'label   = "Workday"' in out
    assert "SCIM provisioning" in out, "NOTE comment must be inserted above resource"


def test_strips_provisioning_block_on_oauth_app():
    hcl = textwrap.dedent('''\
        resource "okta_app_oauth" "internal" {
          label = "Internal"
          type  = "web"

          provisioning {
            scim_enabled = true
          }
        }
        ''')
    out = sanitize_okta_app_scim_refs(_wrap(hcl))["terraform_okta_hcl"]
    assert "provisioning {" not in out
    assert "scim_enabled" not in out
    assert 'label = "Internal"' in out
    assert "SCIM provisioning" in out


def test_strips_single_line_scim_attributes():
    hcl = textwrap.dedent('''\
        resource "okta_app_saml" "x" {
          label             = "X"
          provisioning_type = "SCIM"
          scim_enabled      = true
          scim_url          = "https://x/scim"
        }
        ''')
    out = sanitize_okta_app_scim_refs(_wrap(hcl))["terraform_okta_hcl"]
    assert "provisioning_type" not in out
    assert "scim_enabled" not in out
    assert "scim_url" not in out
    assert 'label             = "X"' in out


def test_does_not_insert_duplicate_note():
    hcl = textwrap.dedent('''\
        # NOTE: SCIM provisioning for this SAML app cannot be configured via the v4.x Okta Terraform provider.
        # Configure it in the Okta Admin Console: Applications -> [App Label] -> Provisioning tab.
        resource "okta_app_saml" "workday" {
          label = "Workday"
          provisioning {
            scim_enabled = true
          }
        }
        ''')
    out = sanitize_okta_app_scim_refs(_wrap(hcl))["terraform_okta_hcl"]
    note_count = out.count("SCIM provisioning for this")
    assert note_count == 1, f"expected exactly one NOTE comment, got {note_count}: {out!r}"


def test_no_op_when_no_provisioning_block():
    hcl = textwrap.dedent('''\
        resource "okta_app_saml" "workday" {
          label                    = "Workday"
          sso_url                  = var.workday_sso_url
          recipient                = var.workday_sso_url
          destination              = var.workday_sso_url
          audience                 = var.workday_audience
          signature_algorithm      = "RSA_SHA256"
        }
        ''')
    out = sanitize_okta_app_scim_refs(_wrap(hcl))["terraform_okta_hcl"]
    assert out == hcl, "input without provisioning must be returned byte-for-byte unchanged"


def test_no_op_when_no_app_resource():
    hcl = textwrap.dedent('''\
        resource "okta_group" "engineering" {
          name = "Engineering"
        }
        ''')
    out = sanitize_okta_app_scim_refs(_wrap(hcl))["terraform_okta_hcl"]
    assert out == hcl


def test_idempotent():
    hcl = textwrap.dedent('''\
        resource "okta_app_saml" "workday" {
          label = "Workday"
          provisioning {
            scim_enabled = true
          }
        }
        ''')
    once = sanitize_okta_app_scim_refs(_wrap(hcl))["terraform_okta_hcl"]
    twice = sanitize_okta_app_scim_refs({"terraform_okta_hcl": once})["terraform_okta_hcl"]
    assert once == twice, "f(f(x)) must equal f(x) — idempotent contract"


def test_handles_multiple_app_resources():
    hcl = textwrap.dedent('''\
        resource "okta_app_saml" "workday" {
          label = "Workday"
          provisioning {
            scim_enabled = true
          }
        }

        resource "okta_app_oauth" "internal" {
          label = "Internal"
          provisioning {
            scim_url = "https://x/scim"
          }
        }
        ''')
    out = sanitize_okta_app_scim_refs(_wrap(hcl))["terraform_okta_hcl"]
    assert "provisioning {" not in out
    assert "scim_enabled" not in out
    assert "scim_url" not in out
    assert 'resource "okta_app_saml" "workday"' in out
    assert 'resource "okta_app_oauth" "internal"' in out
    assert out.count("SCIM provisioning for this") == 2


def test_does_not_strip_outside_app_resource():
    """Other resources that legitimately use a `provisioning` attribute name
    (none exist in Okta v4.x but other providers might) must not be touched."""
    hcl = textwrap.dedent('''\
        resource "okta_app_saml" "x" {
          label = "X"
          provisioning {
            scim_enabled = true
          }
        }

        resource "some_other_provider_resource" "y" {
          provisioning_type = "keep_this"
        }
        ''')
    out = sanitize_okta_app_scim_refs(_wrap(hcl))["terraform_okta_hcl"]
    assert "scim_enabled" not in out
    assert 'provisioning_type = "keep_this"' in out, \
        "provisioning_type on a non-okta-app resource must not be stripped"


def test_does_not_mutate_input():
    hcl = textwrap.dedent('''\
        resource "okta_app_saml" "workday" {
          label = "Workday"
          provisioning {
            scim_enabled = true
          }
        }
        ''')
    inputs = _wrap(hcl)
    sanitize_okta_app_scim_refs(inputs)
    assert "provisioning {" in inputs["terraform_okta_hcl"], \
        "the original input dict must not be mutated"


def test_runs_on_optional_tf_too():
    inputs = {
        "terraform_okta_hcl": "",
        "optional_tf": textwrap.dedent('''\
            resource "okta_app_saml" "workday" {
              label = "Workday"
              provisioning {
                scim_enabled = true
              }
            }
            '''),
    }
    out = sanitize_okta_app_scim_refs(inputs)
    assert "provisioning {" not in out["optional_tf"]
    assert "SCIM provisioning" in out["optional_tf"]


def test_empty_outputs_dict_noop():
    out = sanitize_okta_app_scim_refs({"terraform_okta_hcl": ""})
    assert out["terraform_okta_hcl"] == ""


_TESTS = [
    test_strips_provisioning_block_on_saml_app,
    test_strips_provisioning_block_on_oauth_app,
    test_strips_single_line_scim_attributes,
    test_does_not_insert_duplicate_note,
    test_no_op_when_no_provisioning_block,
    test_no_op_when_no_app_resource,
    test_idempotent,
    test_handles_multiple_app_resources,
    test_does_not_strip_outside_app_resource,
    test_does_not_mutate_input,
    test_runs_on_optional_tf_too,
    test_empty_outputs_dict_noop,
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
