"""Tests for `generator.multi_object_sanitizer`.

Standalone-runnable: `python tests/test_multi_object_sanitizer.py`.
"""

from __future__ import annotations

import os
import re
import sys
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from generator.multi_object_sanitizer import sanitize_multi_object


def _wrap(hcl: str, instance_names: list[str]) -> tuple[dict, dict]:
    return (
        {"terraform_okta_hcl": hcl},
        {"instances": [{"name": n} for n in instance_names]},
    )


def _count_blocks(hcl: str, rtype: str) -> int:
    return len(re.findall(rf'resource\s+"{re.escape(rtype)}"\s+"', hcl))


# ── Core cases ─────────────────────────────────────────────────────────────


def test_clones_one_block_to_three_for_jf10_shape():
    """JF10 shape: LLM emitted 1 of 3 groups. Sanitizer must clone to 3."""
    hcl = textwrap.dedent('''\
        resource "jamfpro_smart_computer_group_v2" "engineering_macs" {
          name = "Engineering Macs"
          criteria {
            name     = "Operating System"
            value    = "macOS"
            priority = 0
          }
        }
        ''')
    outputs = {"terraform_jamf_hcl": hcl}
    intent = {"instances": [
        {"name": "Engineering Macs"},
        {"name": "Sales Macs"},
        {"name": "Marketing Macs"},
    ]}
    out = sanitize_multi_object(outputs, intent)["terraform_jamf_hcl"]
    assert _count_blocks(out, "jamfpro_smart_computer_group_v2") == 3
    assert 'name = "Engineering Macs"' in out
    assert 'name = "Sales Macs"' in out
    assert 'name = "Marketing Macs"' in out


def test_clones_preserve_nested_blocks():
    """Nested sub-blocks must survive cloning (no partial copy)."""
    hcl = textwrap.dedent('''\
        resource "okta_group" "hr" {
          name        = "HR"
          description = "HR department"
        }
        ''')
    outputs, intent = _wrap(hcl, ["HR", "Finance", "Executives"])
    out = sanitize_multi_object(outputs, intent)["terraform_okta_hcl"]
    assert _count_blocks(out, "okta_group") == 3
    assert out.count('description = "HR department"') == 3  # cloned verbatim


def test_already_correct_emission_is_noop():
    """LLM already emitted all 3 blocks. Sanitizer must not duplicate."""
    hcl = textwrap.dedent('''\
        resource "okta_group" "hr" {
          name = "HR"
        }

        resource "okta_group" "finance" {
          name = "Finance"
        }

        resource "okta_group" "executives" {
          name = "Executives"
        }
        ''')
    outputs, intent = _wrap(hcl, ["HR", "Finance", "Executives"])
    out = sanitize_multi_object(outputs, intent)["terraform_okta_hcl"]
    assert out == hcl, "no-op expected when emission is already correct"


def test_partial_emission_filled_in():
    """LLM emitted 2 of 3 blocks. Sanitizer must add the missing 3rd."""
    hcl = textwrap.dedent('''\
        resource "okta_group" "hr" {
          name = "HR"
        }

        resource "okta_group" "finance" {
          name = "Finance"
        }
        ''')
    outputs, intent = _wrap(hcl, ["HR", "Finance", "Executives"])
    out = sanitize_multi_object(outputs, intent)["terraform_okta_hcl"]
    assert _count_blocks(out, "okta_group") == 3
    assert 'name = "HR"' in out
    assert 'name = "Finance"' in out
    assert 'name = "Executives"' in out


def test_clone_label_is_slugified():
    """The cloned block's resource label is derived from the new name."""
    hcl = textwrap.dedent('''\
        resource "okta_group" "engineering_macs" {
          name = "Engineering Macs"
        }
        ''')
    outputs = {"terraform_okta_hcl": hcl}
    intent = {"instances": [
        {"name": "Engineering Macs"},
        {"name": "Sales Macs"},
    ]}
    out = sanitize_multi_object(outputs, intent)["terraform_okta_hcl"]
    assert 'resource "okta_group" "sales_macs"' in out


def test_no_instances_is_noop():
    hcl = 'resource "okta_group" "a" {\n  name = "A"\n}\n'
    outputs = {"terraform_okta_hcl": hcl}
    out = sanitize_multi_object(outputs, {})["terraform_okta_hcl"]
    assert out == hcl


def test_single_instance_is_noop():
    """Only 1 instance: not a multi-object case."""
    hcl = 'resource "okta_group" "a" {\n  name = "A"\n}\n'
    outputs, intent = _wrap(hcl, ["A"])
    out = sanitize_multi_object(outputs, intent)["terraform_okta_hcl"]
    assert out == hcl


def test_no_matching_block_is_noop():
    """LLM emitted a block but its name doesn't match any instance. Conservative:
    leave alone (we don't know which block to use as template)."""
    hcl = 'resource "okta_group" "x" {\n  name = "Unrelated"\n}\n'
    outputs, intent = _wrap(hcl, ["HR", "Finance"])
    out = sanitize_multi_object(outputs, intent)["terraform_okta_hcl"]
    assert out == hcl


def test_idempotent():
    """Running the sanitizer twice produces the same result."""
    hcl = textwrap.dedent('''\
        resource "okta_group" "hr" {
          name = "HR"
        }
        ''')
    outputs, intent = _wrap(hcl, ["HR", "Finance", "Executives"])
    once = sanitize_multi_object(outputs, intent)
    twice = sanitize_multi_object(once, intent)
    assert once["terraform_okta_hcl"] == twice["terraform_okta_hcl"]


def test_input_dict_not_mutated():
    hcl = 'resource "okta_group" "a" {\n  name = "A"\n}\n'
    outputs, intent = _wrap(hcl, ["A", "B"])
    original = outputs["terraform_okta_hcl"]
    sanitize_multi_object(outputs, intent)
    assert outputs["terraform_okta_hcl"] == original


if __name__ == "__main__":
    import traceback
    failures = []
    tests = [
        test_clones_one_block_to_three_for_jf10_shape,
        test_clones_preserve_nested_blocks,
        test_already_correct_emission_is_noop,
        test_partial_emission_filled_in,
        test_clone_label_is_slugified,
        test_no_instances_is_noop,
        test_single_instance_is_noop,
        test_no_matching_block_is_noop,
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
