"""Tests for `generator.okta_event_hook_sanitizer`.

Standalone-runnable: `python tests/test_okta_event_hook_sanitizer.py`.
"""

from __future__ import annotations

import os
import sys
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from generator.okta_event_hook_sanitizer import sanitize_okta_event_hook_events


def _wrap(hcl: str, user_input: str) -> tuple[dict, dict]:
    return {"terraform_okta_hcl": hcl}, {"user_input": user_input}


def _hook_with_events(events: str) -> str:
    return textwrap.dedent(f'''\
        resource "okta_event_hook" "example" {{
          name   = "Example"
          status = "ACTIVE"

          channel = {{
            version = "1.0.0"
            uri     = var.event_hook_url
            type    = "HTTP"
          }}

          events = {events}

          headers {{
            key   = "Authorization"
            value = "Bearer ${{var.token}}"
          }}
        }}
        ''')


def _extract_events(hcl: str) -> str:
    for line in hcl.splitlines():
        stripped = line.strip()
        if stripped.startswith("events"):
            return stripped
    return ""


# ── Failure-mode cases (the 6 regressions this sanitizer closes) ───────────


def test_eh02_transition_rewrites_to_add():
    """EH02: 'Whenever a user joins the Admin group, automatically remove them
    from the Read-Only group' — trigger is ADD even though prompt mentions
    remove."""
    outputs, intent = _wrap(
        _hook_with_events('["group.user_membership.add", "user.lifecycle.create"]'),
        "Whenever a user joins the Admin group, automatically remove them from the Read-Only group",
    )
    result = sanitize_okta_event_hook_events(outputs, intent)
    events_line = _extract_events(result["terraform_okta_hcl"])
    assert events_line == 'events = ["group.user_membership.add"]', f"got: {events_line!r}"


def test_eh04_remove_from_group():
    """EH04: 'Set up an event hook for when users are removed from the Admins
    group' — trigger is REMOVE."""
    outputs, intent = _wrap(
        _hook_with_events('["user.lifecycle.delete.initiated"]'),
        "Set up an event hook for when users are removed from the Admins group",
    )
    result = sanitize_okta_event_hook_events(outputs, intent)
    events_line = _extract_events(result["terraform_okta_hcl"])
    assert events_line == 'events = ["group.user_membership.remove"]', f"got: {events_line!r}"


def test_eh10_profile_updated():
    """EH10: 'Notify an external system when a user's Okta profile is updated'
    — trigger is user.account.update_profile."""
    outputs, intent = _wrap(
        _hook_with_events('["user.lifecycle.update"]'),
        "Notify an external system when a user's Okta profile is updated",
    )
    result = sanitize_okta_event_hook_events(outputs, intent)
    events_line = _extract_events(result["terraform_okta_hcl"])
    assert events_line == 'events = ["user.account.update_profile"]', f"got: {events_line!r}"


def test_ed05_exclusivity_tier_groups():
    """ED05: 'Enforce that users can only be in one of: Free, Pro, or
    Enterprise tier group' — trigger is ADD."""
    outputs, intent = _wrap(
        _hook_with_events('["user.lifecycle.create", "user.lifecycle.update"]'),
        "Enforce that users can only be in one of: Free, Pro, or Enterprise tier group",
    )
    result = sanitize_okta_event_hook_events(outputs, intent)
    events_line = _extract_events(result["terraform_okta_hcl"])
    assert events_line == 'events = ["group.user_membership.add"]', f"got: {events_line!r}"


def test_ehx03_exclusivity_role_groups():
    """EHX03: 'Enforce that a user can only be in one Tableau role group at a
    time: Creator, Explorer, or Viewer' — trigger is ADD."""
    outputs, intent = _wrap(
        _hook_with_events('["user.lifecycle.create", "user.account.update_profile"]'),
        "Enforce that a user can only be in one Tableau role group at a time: Creator, Explorer, or Viewer",
    )
    result = sanitize_okta_event_hook_events(outputs, intent)
    events_line = _extract_events(result["terraform_okta_hcl"])
    assert events_line == 'events = ["group.user_membership.add"]', f"got: {events_line!r}"


def test_awx03_added_to_offboarding_group():
    """AWX03: 'Set up a Lambda that fires when a user is added to the
    Offboarding group...' — trigger is ADD (the group name does not change
    the trigger)."""
    outputs, intent = _wrap(
        _hook_with_events('["user.lifecycle.create"]'),
        "Set up a Lambda that fires when a user is added to the Offboarding group and sends an SNS notification to the security team",
    )
    result = sanitize_okta_event_hook_events(outputs, intent)
    events_line = _extract_events(result["terraform_okta_hcl"])
    assert events_line == 'events = ["group.user_membership.add"]', f"got: {events_line!r}"


# ── Regression cases (currently-passing prompts must stay green) ───────────


def test_eh06_user_deactivated():
    outputs, intent = _wrap(
        _hook_with_events('["user.lifecycle.deactivate"]'),
        "Create an event hook that fires when a user is deactivated",
    )
    result = sanitize_okta_event_hook_events(outputs, intent)
    events_line = _extract_events(result["terraform_okta_hcl"])
    assert events_line == 'events = ["user.lifecycle.deactivate"]', f"got: {events_line!r}"


def test_ehx02_password_change():
    outputs, intent = _wrap(
        _hook_with_events('["user.account.update_password"]'),
        "Set up a webhook that triggers when a user changes their password",
    )
    result = sanitize_okta_event_hook_events(outputs, intent)
    events_line = _extract_events(result["terraform_okta_hcl"])
    assert events_line == 'events = ["user.account.update_password"]', f"got: {events_line!r}"


def test_ehx05_account_activated():
    outputs, intent = _wrap(
        _hook_with_events('["user.lifecycle.activate"]'),
        "Create a webhook triggered when a user account is activated in Okta",
    )
    result = sanitize_okta_event_hook_events(outputs, intent)
    events_line = _extract_events(result["terraform_okta_hcl"])
    assert events_line == 'events = ["user.lifecycle.activate"]', f"got: {events_line!r}"


# ── Edge cases ─────────────────────────────────────────────────────────────


def test_no_event_hook_block_is_noop():
    hcl = 'resource "okta_group" "engineering" {\n  name = "Engineering"\n}\n'
    outputs, intent = _wrap(hcl, "Create a group called Engineering")
    result = sanitize_okta_event_hook_events(outputs, intent)
    assert result["terraform_okta_hcl"] == hcl


def test_missing_user_input_is_noop():
    hcl = _hook_with_events('["user.lifecycle.update"]')
    outputs = {"terraform_okta_hcl": hcl}
    result = sanitize_okta_event_hook_events(outputs, {})
    assert result["terraform_okta_hcl"] == hcl


def test_no_rule_match_is_noop():
    """Prompt with no recognised language pattern leaves events untouched."""
    hcl = _hook_with_events('["user.account.update_password"]')
    outputs, intent = _wrap(hcl, "Set up an event hook for something undefined")
    result = sanitize_okta_event_hook_events(outputs, intent)
    assert result["terraform_okta_hcl"] == hcl


def test_idempotent():
    outputs, intent = _wrap(
        _hook_with_events('["user.lifecycle.update"]'),
        "Notify an external system when a user's Okta profile is updated",
    )
    once = sanitize_okta_event_hook_events(outputs, intent)
    twice = sanitize_okta_event_hook_events(once, intent)
    assert once["terraform_okta_hcl"] == twice["terraform_okta_hcl"]


def test_input_dict_not_mutated():
    """The input outputs dict must not be mutated; a new dict is returned."""
    outputs, intent = _wrap(
        _hook_with_events('["user.lifecycle.update"]'),
        "Notify an external system when a user's Okta profile is updated",
    )
    original_hcl = outputs["terraform_okta_hcl"]
    sanitize_okta_event_hook_events(outputs, intent)
    assert outputs["terraform_okta_hcl"] == original_hcl


def test_awx03_auth_block_rewritten_to_map_attribute():
    """AWX03 emitted `auth { type = ... }` as a bare block; the v4.x provider
    declares `auth` as a map attribute, so terraform validate fails with
    'Unsupported block type'. Sanitizer rewrites to `auth = { ... }`."""
    hcl = textwrap.dedent('''\
        resource "okta_event_hook" "offboarding_group_user_added" {
          name   = "Offboarding Group User Added"
          status = "ACTIVE"

          channel = {
            version = "1.0.0"
            uri     = var.event_hook_url
            type    = "HTTP"
          }

          events = ["group.user_membership.add"]

          auth {
            type = "OAUTH_TWO_LEGGED"
            key  = var.event_hook_oauth_client_id
          }
        }
        ''')
    outputs, intent = _wrap(
        hcl,
        "Set up a Lambda that fires when a user is added to the Offboarding group",
    )
    out = sanitize_okta_event_hook_events(outputs, intent)["terraform_okta_hcl"]
    # Block syntax (no `=`) must be gone.
    assert "auth {" not in out, f"bare block syntax must be rewritten; got: {out!r}"
    # Map attribute syntax must be present.
    assert "auth = {" in out, f"map attribute syntax expected; got: {out!r}"
    # Block contents are preserved.
    assert 'type = "OAUTH_TWO_LEGGED"' in out
    assert "key  = var.event_hook_oauth_client_id" in out


def test_comp09_create_event_wins_over_onboarding_substring():
    """COMP09: 'Set up a complete onboarding workflow ... event hook that fires
    when a new user is created in Okta'. The 'onboarding' substring must NOT
    route to activation — the explicit 'new user is created' phrase wins."""
    outputs, intent = _wrap(
        _hook_with_events('["user.lifecycle.activate"]'),
        "Set up a complete onboarding workflow: create groups for Engineering, "
        "Sales, and HR. Create group rules that auto-assign users to each group "
        "based on their department attribute. Create a SAML app for Workday with "
        "attribute statements for department and manager, and assign all three "
        "groups to it. Add an event hook that fires when a new user is created "
        "in Okta and notifies a Lambda for downstream provisioning.",
    )
    result = sanitize_okta_event_hook_events(outputs, intent)
    events_line = _extract_events(result["terraform_okta_hcl"])
    assert events_line == 'events = ["user.lifecycle.create"]', f"got: {events_line!r}"


def test_onboarding_workflow_alone_does_not_match_activation():
    """Bare 'onboarding workflow' describes a process; not a user-lifecycle
    activation trigger. No rule should match."""
    outputs, intent = _wrap(
        _hook_with_events('["something"]'),
        "Run an onboarding workflow for new hires",
    )
    result = sanitize_okta_event_hook_events(outputs, intent)
    # No match -> sanitizer leaves events untouched.
    events_line = _extract_events(result["terraform_okta_hcl"])
    assert events_line == 'events = ["something"]', f"got: {events_line!r}"


def test_channel_bare_block_rewritten_to_map():
    """Defensive: same rewrite for `channel {}` block syntax even though the
    prompt example uses correct `channel = {}` syntax."""
    hcl = textwrap.dedent('''\
        resource "okta_event_hook" "x" {
          name   = "X"
          status = "ACTIVE"

          channel {
            version = "1.0.0"
            uri     = "https://example.com"
            type    = "HTTP"
          }

          events = ["user.lifecycle.create"]
        }
        ''')
    outputs, intent = _wrap(hcl, "a new user is created in the directory")
    out = sanitize_okta_event_hook_events(outputs, intent)["terraform_okta_hcl"]
    assert "channel {" not in out
    assert "channel = {" in out


if __name__ == "__main__":
    import traceback
    failures = []
    tests = [
        test_eh02_transition_rewrites_to_add,
        test_eh04_remove_from_group,
        test_eh10_profile_updated,
        test_ed05_exclusivity_tier_groups,
        test_ehx03_exclusivity_role_groups,
        test_awx03_added_to_offboarding_group,
        test_eh06_user_deactivated,
        test_ehx02_password_change,
        test_ehx05_account_activated,
        test_no_event_hook_block_is_noop,
        test_missing_user_input_is_noop,
        test_no_rule_match_is_noop,
        test_idempotent,
        test_input_dict_not_mutated,
        test_awx03_auth_block_rewritten_to_map_attribute,
        test_comp09_create_event_wins_over_onboarding_substring,
        test_onboarding_workflow_alone_does_not_match_activation,
        test_channel_bare_block_rewritten_to_map,
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
