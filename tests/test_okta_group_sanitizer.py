"""Tests for `generator.okta_group_sanitizer`.

Standalone-runnable: `python tests/test_okta_group_sanitizer.py` reports
PASS/FAIL per test without any pytest dependency. Pytest will also discover
these tests if installed (each test_* function uses bare `assert`).
"""

from __future__ import annotations

import os
import sys
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from generator.okta_group_sanitizer import sanitize_okta_group_refs


ENGINEERING = {"id": "00g12e0zrkkkfRruT698", "name": "Engineering"}
EVERYONE = {"id": "00g0everyoneXXX", "name": "Everyone"}
FINANCE = {"id": "00g2def", "name": "Finance"}


def test_name_in_live_list_unchanged():
    hcl = textwrap.dedent('''\
        # Resolved from live environment, id: 00g12e0zrkkkfRruT698
        data "okta_group" "engineering" {
          name = "Engineering"
        }
        ''')
    out = sanitize_okta_group_refs(
        {"terraform_okta_hcl": hcl},
        [ENGINEERING],
    )
    assert out["terraform_okta_hcl"] == hcl, \
        "valid data block (name in live list) must be left alone"


def test_name_not_in_live_list_rewritten():
    hcl = textwrap.dedent('''\
        # Resolved from live environment, id: 00g127sgegbdQRQoX698
        data "okta_group" "hr" {
          name = "HR"
        }
        ''')
    out = sanitize_okta_group_refs(
        {"terraform_okta_hcl": hcl},
        [ENGINEERING],
    )
    text = out["terraform_okta_hcl"]
    assert 'resource "okta_group" "hr"' in text, \
        f"expected resource block, got: {text!r}"
    assert 'name        = "HR"' in text, \
        f"expected aligned name attribute, got: {text!r}"
    assert 'description = "HR group"' in text, \
        f"expected default description, got: {text!r}"
    assert 'data "okta_group" "hr"' not in text, \
        "old data block must be gone"
    assert 'Resolved from live environment' not in text, \
        "fabricated resolved-id comment must be stripped"


def test_references_rewritten_when_block_rewritten():
    hcl = textwrap.dedent('''\
        data "okta_group" "hr" {
          name = "HR"
        }

        resource "okta_app_group_assignment" "x" {
          app_id   = okta_app_saml.foo.id
          group_id = data.okta_group.hr.id
          depends_on = [data.okta_group.hr]
        }
        ''')
    out = sanitize_okta_group_refs(
        {"terraform_okta_hcl": hcl},
        [ENGINEERING],
    )
    text = out["terraform_okta_hcl"]
    assert 'group_id = okta_group.hr.id' in text, \
        f"`.id` reference must be rewritten, got: {text!r}"
    assert '[okta_group.hr]' in text, \
        f"bare reference (in depends_on) must be rewritten, got: {text!r}"
    assert 'data.okta_group.hr' not in text, \
        "no data.okta_group.hr references should remain"


def test_references_preserved_when_block_kept():
    hcl = textwrap.dedent('''\
        data "okta_group" "engineering" {
          name = "Engineering"
        }

        output "eng_id" {
          value = data.okta_group.engineering.id
        }
        ''')
    out = sanitize_okta_group_refs(
        {"terraform_okta_hcl": hcl},
        [ENGINEERING],
    )
    text = out["terraform_okta_hcl"]
    assert 'data.okta_group.engineering.id' in text, \
        "valid data ref must NOT be rewritten"
    assert 'data "okta_group" "engineering"' in text, \
        "valid data block must be preserved"


def test_empty_live_groups_noop():
    hcl = 'data "okta_group" "made_up" {\n  name = "MadeUp"\n}\n'
    out = sanitize_okta_group_refs(
        {"terraform_okta_hcl": hcl},
        [],
    )
    assert out["terraform_okta_hcl"] == hcl, \
        "empty live_groups means no ground truth, must be no-op"


def test_none_live_groups_noop():
    hcl = 'data "okta_group" "made_up" {\n  name = "MadeUp"\n}\n'
    out = sanitize_okta_group_refs(
        {"terraform_okta_hcl": hcl},
        None,
    )
    assert out["terraform_okta_hcl"] == hcl, \
        "None live_groups means no ground truth, must be no-op"


def test_multiple_blocks_mixed_validity():
    hcl = textwrap.dedent('''\
        data "okta_group" "engineering" {
          name = "Engineering"
        }

        data "okta_group" "finance" {
          name = "Finance"
        }

        data "okta_group" "hr" {
          name = "HR"
        }

        resource "okta_app_group_assignment" "a" {
          app_id   = okta_app_saml.x.id
          group_id = data.okta_group.engineering.id
        }

        resource "okta_app_group_assignment" "b" {
          app_id   = okta_app_saml.x.id
          group_id = data.okta_group.finance.id
        }

        resource "okta_app_group_assignment" "c" {
          app_id   = okta_app_saml.x.id
          group_id = data.okta_group.hr.id
        }
        ''')
    out = sanitize_okta_group_refs(
        {"terraform_okta_hcl": hcl},
        [ENGINEERING, FINANCE],
    )
    text = out["terraform_okta_hcl"]

    assert 'data "okta_group" "engineering"' in text
    assert 'data "okta_group" "finance"' in text
    assert 'data "okta_group" "hr"' not in text
    assert 'resource "okta_group" "hr"' in text

    assert 'data.okta_group.engineering.id' in text
    assert 'data.okta_group.finance.id' in text
    assert 'okta_group.hr.id' in text
    assert 'data.okta_group.hr.id' not in text


def test_idempotent():
    hcl = textwrap.dedent('''\
        # Resolved from live environment, id: 00g127sgegbdQRQoX698
        data "okta_group" "hr" {
          name = "HR"
        }

        resource "okta_app_group_assignment" "x" {
          group_id = data.okta_group.hr.id
        }
        ''')
    once = sanitize_okta_group_refs(
        {"terraform_okta_hcl": hcl},
        [ENGINEERING],
    )
    twice = sanitize_okta_group_refs(once, [ENGINEERING])
    assert once["terraform_okta_hcl"] == twice["terraform_okta_hcl"], \
        "f(f(x)) must equal f(x) — idempotent contract"


def test_unfamiliar_block_structure_left_alone():
    hcl = textwrap.dedent('''\
        data "okta_group" "weird" {
          name = "WeirdName"
          type = "OKTA_GROUP"
        }
        ''')
    out = sanitize_okta_group_refs(
        {"terraform_okta_hcl": hcl},
        [ENGINEERING],
    )
    assert out["terraform_okta_hcl"] == hcl, \
        "blocks with extra attributes are conservatively left untouched"


def test_resolved_id_comment_stripped_only_when_block_rewritten():
    hcl_invalid = textwrap.dedent('''\
        # Resolved from live environment, id: 00g127sgegbdQRQoX698
        data "okta_group" "hr" {
          name = "HR"
        }
        ''')
    out_invalid = sanitize_okta_group_refs(
        {"terraform_okta_hcl": hcl_invalid},
        [ENGINEERING],
    )
    assert 'Resolved from live environment' not in out_invalid["terraform_okta_hcl"], \
        "comment above rewritten block must be stripped"

    hcl_valid = textwrap.dedent('''\
        # Resolved from live environment, id: 00g12e0zrkkkfRruT698
        data "okta_group" "engineering" {
          name = "Engineering"
        }
        ''')
    out_valid = sanitize_okta_group_refs(
        {"terraform_okta_hcl": hcl_valid},
        [ENGINEERING],
    )
    assert 'Resolved from live environment' in out_valid["terraform_okta_hcl"], \
        "comment above kept block must be preserved"


def test_lambda_hcl_references_rewritten():
    okta_hcl = textwrap.dedent('''\
        data "okta_group" "hr" {
          name = "HR"
        }
        ''')
    lambda_hcl = textwrap.dedent('''\
        resource "aws_lambda_function" "alert" {
          environment {
            variables = {
              OKTA_HR_GROUP_ID = data.okta_group.hr.id
            }
          }
        }
        ''')
    out = sanitize_okta_group_refs(
        {
            "terraform_okta_hcl": okta_hcl,
            "terraform_lambda_hcl": lambda_hcl,
        },
        [ENGINEERING],
    )
    assert 'OKTA_HR_GROUP_ID = okta_group.hr.id' in out["terraform_lambda_hcl"], \
        "cross-file references must also be rewritten"
    assert 'data.okta_group.hr' not in out["terraform_lambda_hcl"], \
        "no stale data references should remain in lambda HCL"


def test_outputs_dict_not_mutated_in_place():
    hcl = 'data "okta_group" "hr" {\n  name = "HR"\n}\n'
    original_outputs = {"terraform_okta_hcl": hcl, "lambda_python": "stub"}
    snapshot_hcl = original_outputs["terraform_okta_hcl"]

    new_outputs = sanitize_okta_group_refs(original_outputs, [ENGINEERING])

    assert original_outputs["terraform_okta_hcl"] == snapshot_hcl, \
        "input dict must not be mutated in place"
    assert new_outputs is not original_outputs, \
        "function must return a new dict, not the same reference"
    assert new_outputs["terraform_okta_hcl"] != snapshot_hcl, \
        "new dict must contain the rewritten HCL"


_TESTS = [
    test_name_in_live_list_unchanged,
    test_name_not_in_live_list_rewritten,
    test_references_rewritten_when_block_rewritten,
    test_references_preserved_when_block_kept,
    test_empty_live_groups_noop,
    test_none_live_groups_noop,
    test_multiple_blocks_mixed_validity,
    test_idempotent,
    test_unfamiliar_block_structure_left_alone,
    test_resolved_id_comment_stripped_only_when_block_rewritten,
    test_lambda_hcl_references_rewritten,
    test_outputs_dict_not_mutated_in_place,
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
