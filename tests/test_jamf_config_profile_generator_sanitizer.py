"""Tests for `generator.jamf_config_profile_generator_sanitizer`.

Standalone-runnable:
    python tests/test_jamf_config_profile_generator_sanitizer.py
"""

from __future__ import annotations

import os
import sys
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from generator.jamf_config_profile_generator_sanitizer import (
    sanitize_jamf_config_profile_generator,
)


REQUIRED_HEADERS = (
    "payload_description_header",
    "payload_enabled_header",
    "payload_organization_header",
    "payload_type_header",
    "payload_version_header",
)

REQUIRED_CONTENT_FIELDS = (
    "payload_enabled",
    "payload_organization",
    "payload_type",
    "payload_version",
)


def _bare_profile(payloads_body: str) -> str:
    """Wrap a payloads-block body in a minimum-viable profile resource.

    The `payloads_body` argument is indented to 4 spaces (the depth inside
    `payloads {}`) so callers can pass plain dedented blocks without having
    to manage indentation by hand.
    """
    # Indent the body to 4 spaces (inside `payloads {}`).
    indented = textwrap.indent(textwrap.dedent(payloads_body), "    ")
    return (
        'resource "jamfpro_macos_configuration_profile_plist_generator" "corp_wifi" {\n'
        '  name               = "Corp Wi-Fi (generated)"\n'
        '  redeploy_on_update = "Newly Assigned"\n'
        '\n'
        '  payloads {\n'
        f'{indented}'
        '  }\n'
        '\n'
        '  scope {\n'
        '    all_computers = true\n'
        '  }\n'
        '}\n'
    )


# ── Positive drift cases ───────────────────────────────────────────────────


def test_missing_all_headers_and_content_filled():
    """JF04 canonical drift: payloads {} block is essentially empty. The
    sanitizer must auto-insert all 5 header attrs AND the payload_content {}
    sub-block."""
    hcl = _bare_profile("    # empty payloads body\n")
    result = sanitize_jamf_config_profile_generator({"terraform_jamf_hcl": hcl})
    out = result["terraform_jamf_hcl"]
    for header in REQUIRED_HEADERS:
        assert header in out, f"missing header after sanitize: {header}"
    assert "payload_content {" in out
    for field in REQUIRED_CONTENT_FIELDS:
        assert field in out, f"missing payload_content field: {field}"


def test_missing_single_header_filled():
    """Only `payload_organization_header` is missing; only that one is auto-filled."""
    hcl = _bare_profile(textwrap.dedent('''\
            payload_description_header  = "Corp Wi-Fi"
            payload_enabled_header      = true
            payload_type_header         = "Configuration"
            payload_version_header      = 1

            payload_content {
              payload_enabled      = true
              payload_organization = "Example Corp"
              payload_type         = "com.apple.wifi.managed"
              payload_version      = 1
            }
        '''))
    result = sanitize_jamf_config_profile_generator({"terraform_jamf_hcl": hcl})
    out = result["terraform_jamf_hcl"]
    assert "payload_organization_header" in out
    # The other 4 headers must still be present.
    for header in REQUIRED_HEADERS:
        assert header in out, f"missing header: {header}"


def test_missing_payload_content_block_inserted():
    """All 5 headers present, but `payload_content {}` sub-block missing."""
    hcl = _bare_profile(textwrap.dedent('''\
            payload_description_header  = "Corp Wi-Fi"
            payload_enabled_header      = true
            payload_organization_header = "Example Corp"
            payload_type_header         = "Configuration"
            payload_version_header      = 1
        '''))
    result = sanitize_jamf_config_profile_generator({"terraform_jamf_hcl": hcl})
    out = result["terraform_jamf_hcl"]
    assert "payload_content {" in out
    for field in REQUIRED_CONTENT_FIELDS:
        assert field in out, f"missing payload_content field: {field}"


def test_partial_payload_content_missing_field_filled():
    """payload_content {} exists but is missing one required field
    (payload_version). Sanitizer fills the missing field in place."""
    hcl = _bare_profile(textwrap.dedent('''\
            payload_description_header  = "Corp Wi-Fi"
            payload_enabled_header      = true
            payload_organization_header = "Example Corp"
            payload_type_header         = "Configuration"
            payload_version_header      = 1

            payload_content {
              payload_enabled      = true
              payload_organization = "Example Corp"
              payload_type         = "com.apple.wifi.managed"
            }
        '''))
    result = sanitize_jamf_config_profile_generator({"terraform_jamf_hcl": hcl})
    out = result["terraform_jamf_hcl"]
    assert "payload_version " in out or "payload_version=" in out


def test_auto_fill_comment_present():
    """Auto-inserted lines must carry the Phase 20 sanitizer marker."""
    hcl = _bare_profile("    # empty\n")
    result = sanitize_jamf_config_profile_generator({"terraform_jamf_hcl": hcl})
    out = result["terraform_jamf_hcl"]
    assert "auto-filled by Phase 20 sanitizer" in out


# ── Negative / idempotent cases ────────────────────────────────────────────


def test_clean_profile_unchanged():
    """A profile that already has all 5 headers and payload_content {} is left alone."""
    hcl = textwrap.dedent('''\
        resource "jamfpro_macos_configuration_profile_plist_generator" "clean" {
          name               = "Clean"
          redeploy_on_update = "Newly Assigned"

          payloads {
            payload_description_header  = "Clean profile"
            payload_enabled_header      = true
            payload_organization_header = "Example Corp"
            payload_type_header         = "Configuration"
            payload_version_header      = 1

            payload_content {
              payload_enabled      = true
              payload_organization = "Example Corp"
              payload_type         = "com.apple.wifi.managed"
              payload_version      = 1
            }
          }

          scope {
            all_computers = true
          }
        }
        ''')
    result = sanitize_jamf_config_profile_generator({"terraform_jamf_hcl": hcl})
    assert result["terraform_jamf_hcl"] == hcl


def test_idempotent():
    hcl = _bare_profile("    # empty\n")
    once = sanitize_jamf_config_profile_generator({"terraform_jamf_hcl": hcl})
    twice = sanitize_jamf_config_profile_generator(once)
    assert once["terraform_jamf_hcl"] == twice["terraform_jamf_hcl"]


def test_no_profile_block_is_noop():
    """A JAMF HCL with no plist_generator resource is left alone."""
    hcl = textwrap.dedent('''\
        resource "jamfpro_policy" "install_slack" {
          name = "Install Slack"
        }
        ''')
    result = sanitize_jamf_config_profile_generator({"terraform_jamf_hcl": hcl})
    assert result["terraform_jamf_hcl"] == hcl


def test_other_resource_blocks_untouched():
    """Only the plist_generator block is rewritten; sibling resources stay
    untouched even if they happen to have a payloads-like substring."""
    hcl = textwrap.dedent('''\
        resource "jamfpro_macos_configuration_profile_plist" "ipad_kiosk" {
          name     = "iPad Kiosk"
          payloads = file("../profiles/ipad_kiosk.mobileconfig")
        }

        resource "jamfpro_macos_configuration_profile_plist_generator" "corp_wifi" {
          name               = "Corp Wi-Fi"
          redeploy_on_update = "Newly Assigned"

          payloads {
          }

          scope {
            all_computers = true
          }
        }
        ''')
    result = sanitize_jamf_config_profile_generator({"terraform_jamf_hcl": hcl})
    out = result["terraform_jamf_hcl"]
    # The plist (non-generator) resource is left alone.
    assert 'payloads = file("../profiles/ipad_kiosk.mobileconfig")' in out
    # The generator block has the 5 headers auto-filled.
    for header in REQUIRED_HEADERS:
        assert header in out


def test_input_dict_not_mutated():
    hcl = _bare_profile("    # empty\n")
    outputs = {"terraform_jamf_hcl": hcl}
    sanitize_jamf_config_profile_generator(outputs)
    assert outputs["terraform_jamf_hcl"] == hcl


if __name__ == "__main__":
    import traceback
    failures = []
    tests = [
        test_missing_all_headers_and_content_filled,
        test_missing_single_header_filled,
        test_missing_payload_content_block_inserted,
        test_partial_payload_content_missing_field_filled,
        test_auto_fill_comment_present,
        test_clean_profile_unchanged,
        test_idempotent,
        test_no_profile_block_is_noop,
        test_other_resource_blocks_untouched,
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
