"""Tests for `fleet_validate.validate_fleet_yaml`.

Standalone-runnable: `python tests/test_fleet_validate.py`.
"""

from __future__ import annotations

import os
import sys
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from fleet_validate import validate_fleet_yaml


_RUNBOOK = textwrap.dedent("""\
    # FLEET GITOPS APPLY RUNBOOK
    # 1. Validate:  fleetctl apply -f default.yml --dry-run
    # 2. Apply:     fleetctl apply -f default.yml
    # Required env: FLEET_URL, FLEET_API_TOKEN
    # Server requirement: Fleet >= 4.82.0
""")


def _with_runbook(body: str) -> str:
    return _RUNBOOK + body


# ── Happy paths ────────────────────────────────────────────────────────────


def test_minimal_valid_policy():
    yml = _with_runbook(textwrap.dedent("""\
        policies:
          - name: macOS - FileVault enabled
            query: "SELECT 1 FROM filevault_status WHERE status = 'FileVault is On.';"
            platform: darwin
    """))
    ok, msg = validate_fleet_yaml(yml)
    assert ok, msg


def test_multiple_platforms_comma_separated():
    yml = _with_runbook(textwrap.dedent("""\
        policies:
          - name: cross-platform check
            query: "SELECT 1;"
            platform: "darwin,linux,windows"
    """))
    ok, msg = validate_fleet_yaml(yml)
    assert ok, msg


def test_dynamic_label_with_query():
    yml = _with_runbook(textwrap.dedent("""\
        labels:
          - name: Arm64
            query: "SELECT 1 FROM system_info WHERE cpu_type LIKE 'arm64%';"
            label_membership_type: dynamic
    """))
    ok, msg = validate_fleet_yaml(yml)
    assert ok, msg


def test_manual_label_with_hosts():
    yml = _with_runbook(textwrap.dedent("""\
        labels:
          - name: C-Suite
            label_membership_type: manual
            hosts:
              - "ABC123"
              - "DEF456"
    """))
    ok, msg = validate_fleet_yaml(yml)
    assert ok, msg


def test_configuration_profile_path():
    yml = _with_runbook(textwrap.dedent("""\
        controls:
          apple_settings:
            configuration_profiles:
              - path: ../lib/macos/profiles/wifi.mobileconfig
    """))
    ok, msg = validate_fleet_yaml(yml)
    assert ok, msg


def test_configuration_profile_paths_glob():
    yml = _with_runbook(textwrap.dedent("""\
        controls:
          windows_settings:
            configuration_profiles:
              - paths: ../lib/windows/profiles/*.xml
    """))
    ok, msg = validate_fleet_yaml(yml)
    assert ok, msg


def test_agent_options_minimal():
    yml = _with_runbook(textwrap.dedent("""\
        agent_options:
          config:
            options:
              distributed_interval: 30
            decorators:
              load:
                - "SELECT uuid AS host_uuid FROM system_info;"
    """))
    ok, msg = validate_fleet_yaml(yml)
    assert ok, msg


def test_query_with_required_interval():
    yml = _with_runbook(textwrap.dedent("""\
        queries:
          - name: chrome-extensions
            query: "SELECT * FROM chrome_extensions;"
            interval: 86400
    """))
    ok, msg = validate_fleet_yaml(yml)
    assert ok, msg


# ── Failure paths ──────────────────────────────────────────────────────────


def test_missing_runbook_header_fails():
    yml = textwrap.dedent("""\
        policies:
          - name: x
            query: "SELECT 1;"
            platform: darwin
    """)
    ok, msg = validate_fleet_yaml(yml)
    assert not ok
    assert "FLEET GITOPS APPLY RUNBOOK" in msg


def test_unknown_top_level_key_fails():
    yml = _with_runbook(textwrap.dedent("""\
        unknown_key:
          - foo
    """))
    ok, msg = validate_fleet_yaml(yml)
    assert not ok
    assert "unknown top-level key" in msg


def test_policy_without_query_fails():
    yml = _with_runbook(textwrap.dedent("""\
        policies:
          - name: bad-policy
            platform: darwin
    """))
    ok, msg = validate_fleet_yaml(yml)
    assert not ok
    assert "missing required field `query`" in msg


def test_policy_with_invalid_platform_fails():
    yml = _with_runbook(textwrap.dedent("""\
        policies:
          - name: bad-platform
            query: "SELECT 1;"
            platform: solaris
    """))
    ok, msg = validate_fleet_yaml(yml)
    assert not ok
    assert "platform" in msg


def test_label_with_query_and_hosts_fails():
    yml = _with_runbook(textwrap.dedent("""\
        labels:
          - name: both
            query: "SELECT 1;"
            hosts: ["X"]
    """))
    ok, msg = validate_fleet_yaml(yml)
    assert not ok
    assert "mutually exclusive" in msg


def test_label_with_no_membership_fails():
    yml = _with_runbook(textwrap.dedent("""\
        labels:
          - name: empty
    """))
    ok, msg = validate_fleet_yaml(yml)
    assert not ok
    assert "exactly one" in msg


def test_configuration_profile_with_path_and_paths_fails():
    yml = _with_runbook(textwrap.dedent("""\
        controls:
          apple_settings:
            configuration_profiles:
              - path: a.mobileconfig
                paths: "*.mobileconfig"
    """))
    ok, msg = validate_fleet_yaml(yml)
    assert not ok
    assert "mutually exclusive" in msg


def test_query_interval_must_be_int():
    yml = _with_runbook(textwrap.dedent("""\
        queries:
          - name: q
            query: "SELECT 1;"
            interval: "86400"
    """))
    ok, msg = validate_fleet_yaml(yml)
    assert not ok
    assert "interval must be an integer" in msg


def test_agent_options_without_config_fails():
    yml = _with_runbook(textwrap.dedent("""\
        agent_options:
          options:
            distributed_interval: 30
    """))
    ok, msg = validate_fleet_yaml(yml)
    assert not ok
    assert "`config` sub-key" in msg


def test_invalid_yaml_syntax_fails():
    yml = _with_runbook("policies:\n  - name: x\n  query: missing-dash\n")
    ok, msg = validate_fleet_yaml(yml)
    assert not ok
    # YAML error OR schema error (depends on which trips first).
    assert msg.startswith("yaml:") or msg.startswith("fleet:")


def test_empty_input_fails():
    ok, msg = validate_fleet_yaml("")
    assert not ok
    assert "empty" in msg.lower()


if __name__ == "__main__":
    import traceback
    failures = []
    tests = [
        test_minimal_valid_policy,
        test_multiple_platforms_comma_separated,
        test_dynamic_label_with_query,
        test_manual_label_with_hosts,
        test_configuration_profile_path,
        test_configuration_profile_paths_glob,
        test_agent_options_minimal,
        test_query_with_required_interval,
        test_missing_runbook_header_fails,
        test_unknown_top_level_key_fails,
        test_policy_without_query_fails,
        test_policy_with_invalid_platform_fails,
        test_label_with_query_and_hosts_fails,
        test_label_with_no_membership_fails,
        test_configuration_profile_with_path_and_paths_fails,
        test_query_interval_must_be_int,
        test_agent_options_without_config_fails,
        test_invalid_yaml_syntax_fails,
        test_empty_input_fails,
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
