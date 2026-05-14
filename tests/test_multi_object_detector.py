"""Tests for `generator.multi_object_detector`.

Standalone-runnable: `python tests/test_multi_object_detector.py`.

Covers the 9 multi-object prompts surfaced from the qa_runner.py test corpus
plus regression / edge cases.
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from generator.multi_object_detector import detect_instances


def _names(result):
    return [d["name"] for d in (result or [])]


# ── 9 corpus cases ─────────────────────────────────────────────────────────


def test_jf10_three_jamf_groups():
    """JF10: 'Create three JAMF smart computer groups: Engineering Macs, Sales Macs, and Marketing Macs.'"""
    out = detect_instances(
        "Create three JAMF smart computer groups: Engineering Macs, Sales Macs, and Marketing Macs."
    )
    assert _names(out) == ["Engineering Macs", "Sales Macs", "Marketing Macs"], f"got: {out}"


def test_comp02_saml_plus_three_groups():
    """COMP02: 'Create a SAML app for Workday and assign three groups: HR, Finance, and Executives'"""
    out = detect_instances(
        "Create a SAML app for Workday and assign three groups: HR, Finance, and Executives"
    )
    assert _names(out) == ["HR", "Finance", "Executives"], f"got: {out}"


def test_comp06_two_scopes():
    """COMP06: 'Create an authorization server for the mobile API with two scopes: read:profile and write:settings, ...'"""
    out = detect_instances(
        "Create an authorization server for the mobile API with two scopes: read:profile and write:settings, "
        "and an access policy limiting token lifetime to 30 minutes"
    )
    assert _names(out) == ["read:profile", "write:settings"], f"got: {out}"


def test_comp10_three_scopes():
    """COMP10: 'Define three scopes: read:data, write:data, and admin:data'"""
    out = detect_instances(
        "Set up a custom authorization server for our internal API. Define three scopes: "
        "read:data, write:data, and admin:data. Add two custom claims..."
    )
    assert _names(out) == ["read:data", "write:data", "admin:data"], f"got: {out}"


def test_sa02_three_groups():
    """SA02: 'Create a SAML app for Salesforce and assign three groups: Sales, Sales Managers, and Sales Ops.'"""
    out = detect_instances(
        "Create a SAML app for Salesforce and assign three groups: Sales, Sales Managers, and Sales Ops. "
        "Sales Managers get a role attribute statement."
    )
    assert _names(out) == ["Sales", "Sales Managers", "Sales Ops"], f"got: {out}"


def test_sc02_two_scopes():
    """SC02: 'Create two scopes on the developer API auth server: read:data and write:data'"""
    out = detect_instances(
        "Create two scopes on the developer API auth server: read:data and write:data"
    )
    assert _names(out) == ["read:data", "write:data"], f"got: {out}"


# ── Edge / regression cases ────────────────────────────────────────────────


def test_no_enumeration_returns_none():
    """Single-object prompt: detector must return None."""
    out = detect_instances("Create a group called Engineering")
    assert out is None, f"expected None for single-object; got: {out}"


def test_empty_input_returns_none():
    assert detect_instances("") is None
    assert detect_instances("   ") is None


def test_count_word_alone_returns_none_without_kind():
    """Bare numeric word with no resource kind: not safe to extract."""
    out = detect_instances("Create three things for the team")
    assert out is None, f"got: {out}"


def test_count_mismatch_skips_candidate():
    """'three groups: A, B' — count says 3 but list has 2. Conservative: skip."""
    out = detect_instances("Create three groups: HR, Finance")
    assert out is None, f"got: {out}"


def test_oxford_comma_preserved():
    out = detect_instances(
        "Set up three policies: alpha, beta, and gamma"
    )
    assert _names(out) == ["alpha", "beta", "gamma"], f"got: {out}"


def test_or_separator_normalized():
    """ED05 shape: 'one of: A, B, or C'. The `or` separator must normalize
    just like `and` — otherwise the third item carries the 'or ' prefix and
    its slug becomes 'or_c' which corrupts label generation downstream."""
    out = detect_instances(
        "Enforce that users can only be in one of: Free, Pro, or Enterprise tier group"
    )
    # The detector matches the "one of:" pattern -> _BARE_LIST_RE OR _COUNT_COLON_RE.
    # Either path must normalize 'or'.
    assert out is not None
    names = _names(out)
    assert "Free" in names
    assert "Pro" in names
    # Third name must not start with "or " — the leading separator must be stripped.
    third = [n for n in names if n.startswith("Enterprise") or n.startswith("or Enterprise")]
    assert third, f"missing Enterprise variant; got: {names}"
    assert not any(n.startswith("or ") for n in names), f"got: {names}"


def test_dashed_separator():
    """'<count> kind - <list>' dash separator should also work."""
    out = detect_instances(
        "Create three groups - Sales, Marketing, and Support"
    )
    assert _names(out) == ["Sales", "Marketing", "Support"], f"got: {out}"


def test_idempotent():
    prompt = "Create three groups: A, B, C"
    a = detect_instances(prompt)
    b = detect_instances(prompt)
    assert _names(a) == _names(b)


def test_long_name_filtered_out():
    """Names over 40 chars are filtered — likely prose tails not group names."""
    long_prompt = (
        "Create three groups: ShortA, ShortB, "
        "and a really really really really really long descriptive sentence that is clearly not a group name"
    )
    out = detect_instances(long_prompt)
    # The list has only 2 valid names so the count check (3 expected) fails,
    # detector returns None per conservative rule.
    assert out is None, f"expected None when count mismatch; got: {out}"


if __name__ == "__main__":
    import traceback
    failures = []
    tests = [
        test_jf10_three_jamf_groups,
        test_comp02_saml_plus_three_groups,
        test_comp06_two_scopes,
        test_comp10_three_scopes,
        test_sa02_three_groups,
        test_sc02_two_scopes,
        test_no_enumeration_returns_none,
        test_empty_input_returns_none,
        test_count_word_alone_returns_none_without_kind,
        test_count_mismatch_skips_candidate,
        test_oxford_comma_preserved,
        test_or_separator_normalized,
        test_dashed_separator,
        test_idempotent,
        test_long_name_filtered_out,
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
