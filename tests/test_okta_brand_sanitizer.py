"""Tests for `generator.okta_brand_sanitizer`.

Standalone-runnable: `python tests/test_okta_brand_sanitizer.py`.
"""

from __future__ import annotations

import os
import sys
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from generator.okta_brand_sanitizer import sanitize_okta_brand_refs


def _wrap(hcl: str) -> dict:
    return {"terraform_okta_hcl": hcl}


def test_strips_logo_attribute():
    hcl = textwrap.dedent('''\
        resource "okta_brand" "default" {
          name                            = "Default"
          agree_to_custom_privacy_policy  = true
          logo                            = "/path/to/logo.png"
        }
        ''')
    out = sanitize_okta_brand_refs(_wrap(hcl))["terraform_okta_hcl"]
    assert "logo" not in out, f"logo line must be stripped, got: {out!r}"
    assert 'name                            = "Default"' in out
    assert 'agree_to_custom_privacy_policy' in out


def test_strips_primary_color_and_secondary_color():
    hcl = textwrap.dedent('''\
        resource "okta_brand" "default" {
          name             = "Default"
          primary_color    = "#1A1A2E"
          secondary_color  = "#2D6A9F"
        }
        ''')
    out = sanitize_okta_brand_refs(_wrap(hcl))["terraform_okta_hcl"]
    assert "primary_color" not in out
    assert "secondary_color" not in out
    assert 'name             = "Default"' in out


def test_strips_logo_block():
    hcl = textwrap.dedent('''\
        resource "okta_brand" "default" {
          name = "Default"
          logo {
            file_path = "/x"
            format    = "png"
          }
        }
        ''')
    out = sanitize_okta_brand_refs(_wrap(hcl))["terraform_okta_hcl"]
    assert "logo {" not in out
    assert "file_path" not in out
    assert 'name = "Default"' in out


def test_no_op_when_okta_brand_absent():
    hcl = textwrap.dedent('''\
        resource "okta_group" "engineering" {
          name = "Engineering"
        }
        ''')
    out = sanitize_okta_brand_refs(_wrap(hcl))["terraform_okta_hcl"]
    assert out == hcl, "input without okta_brand must be returned unchanged"


def test_idempotent():
    hcl = textwrap.dedent('''\
        resource "okta_brand" "default" {
          name = "Default"
          logo = "/x.png"
        }
        ''')
    once = sanitize_okta_brand_refs(_wrap(hcl))["terraform_okta_hcl"]
    twice = sanitize_okta_brand_refs({"terraform_okta_hcl": once})["terraform_okta_hcl"]
    assert once == twice, "f(f(x)) must equal f(x) — idempotent contract"


def test_preserves_allowed_attributes():
    hcl = textwrap.dedent('''\
        resource "okta_brand" "default" {
          name                            = "Default"
          agree_to_custom_privacy_policy  = true
          custom_privacy_policy_url       = "https://example.com/privacy"
          remove_powered_by_okta          = true
          logo                            = "/x.png"
        }
        ''')
    out = sanitize_okta_brand_refs(_wrap(hcl))["terraform_okta_hcl"]
    assert "logo" not in out
    assert 'agree_to_custom_privacy_policy  = true' in out
    assert 'custom_privacy_policy_url       = "https://example.com/privacy"' in out
    assert 'remove_powered_by_okta          = true' in out


def test_handles_multiple_brand_resources():
    hcl = textwrap.dedent('''\
        resource "okta_brand" "default" {
          name = "Default"
          logo = "/a.png"
        }

        resource "okta_brand" "secondary" {
          name = "Secondary"
          primary_color = "#FFF"
        }
        ''')
    out = sanitize_okta_brand_refs(_wrap(hcl))["terraform_okta_hcl"]
    assert "logo" not in out
    assert "primary_color" not in out
    assert 'resource "okta_brand" "default"' in out
    assert 'resource "okta_brand" "secondary"' in out


def test_does_not_strip_logo_outside_brand_resource():
    """A resource that legitimately uses an attribute named `logo` (e.g. some
    hypothetical custom resource) must not be touched. We only strip inside
    okta_brand blocks."""
    hcl = textwrap.dedent('''\
        resource "okta_brand" "default" {
          name = "Default"
          logo = "/strip.png"
        }

        resource "some_other_resource" "x" {
          logo = "/keep.png"
        }
        ''')
    out = sanitize_okta_brand_refs(_wrap(hcl))["terraform_okta_hcl"]
    assert '/strip.png' not in out
    assert '/keep.png' in out


def test_does_not_mutate_input():
    hcl = textwrap.dedent('''\
        resource "okta_brand" "default" {
          name = "Default"
          logo = "/x.png"
        }
        ''')
    inputs = _wrap(hcl)
    sanitize_okta_brand_refs(inputs)
    assert "logo" in inputs["terraform_okta_hcl"], \
        "the original input dict must not be mutated"


def test_runs_on_optional_tf_too():
    inputs = {
        "terraform_okta_hcl": "",
        "optional_tf": textwrap.dedent('''\
            resource "okta_brand" "default" {
              name = "Default"
              secondary_color = "#000"
            }
            '''),
    }
    out = sanitize_okta_brand_refs(inputs)
    assert "secondary_color" not in out["optional_tf"]


def test_empty_outputs_dict_noop():
    out = sanitize_okta_brand_refs({"terraform_okta_hcl": ""})
    assert out["terraform_okta_hcl"] == ""


_TESTS = [
    test_strips_logo_attribute,
    test_strips_primary_color_and_secondary_color,
    test_strips_logo_block,
    test_no_op_when_okta_brand_absent,
    test_idempotent,
    test_preserves_allowed_attributes,
    test_handles_multiple_brand_resources,
    test_does_not_strip_logo_outside_brand_resource,
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
