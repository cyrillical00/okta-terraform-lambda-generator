"""Smoke tests for JAMF prompt content. Standalone-runnable.

Pytest will discover these via the `test_*` function names; running the file
directly (`python tests/test_jamf_prompts.py`) reports PASS/FAIL per case
without any pytest dependency.
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from generator.prompts import GENERATOR_SYSTEM_PROMPT, INTENT_PARSER_SYSTEM_PROMPT
from generator.validator import VALIDATOR_SYSTEM_PROMPT


JAMF_RESOURCES = [
    "jamfpro_policy",
    "jamfpro_script",
    "jamfpro_macos_configuration_profile_plist",
    "jamfpro_macos_configuration_profile_plist_generator",
    "jamfpro_mobile_device_configuration_profile_plist",
    "jamfpro_smart_computer_group_v2",
    "jamfpro_static_computer_group",
    "jamfpro_smart_mobile_device_group",
    "jamfpro_package",
    "jamfpro_computer_extension_attribute",
    "jamfpro_restricted_software",
    "jamfpro_computer_prestage_enrollment",
]


def _section_d() -> str:
    """Extract the SECTION D substring from GENERATOR_SYSTEM_PROMPT.

    SECTION headings are markdown ("## SECTION X"), so we anchor on that exact
    prefix to avoid matching in-prose references like "see SECTION F.5".
    """
    src = GENERATOR_SYSTEM_PROMPT
    idx = src.find("## SECTION D")
    assert idx > 0, "## SECTION D heading must exist in GENERATOR_SYSTEM_PROMPT"
    end = src.find("## SECTION", idx + len("## SECTION D"))
    return src[idx:end if end > 0 else len(src)]


def test_section_d_present():
    assert "SECTION D" in GENERATOR_SYSTEM_PROMPT, \
        "SECTION D heading is missing from GENERATOR_SYSTEM_PROMPT"
    assert "## SECTION D" in GENERATOR_SYSTEM_PROMPT, \
        "SECTION D must use the markdown ## heading style"
    assert "JAMF Pro" in GENERATOR_SYSTEM_PROMPT, \
        "JAMF Pro provider rules content is missing"


def test_provider_pinned():
    text = _section_d()
    assert "deploymenttheory/jamfpro" in text, \
        "Provider source pin missing or wrong"
    assert "~> 0.37" in text, \
        "Provider version constraint must be ~> 0.37"


def test_yohan460_explicitly_rejected():
    text = _section_d()
    assert "yohan460" in text, \
        "yohan460/jamf must be explicitly named so the model knows to reject it"
    lower = text.lower()
    rejected_signal = any(word in lower for word in ("do not", "stale", "reject", "deprecated"))
    assert rejected_signal, \
        "yohan460 must appear with explicit reject-language (do not / stale / reject / deprecated)"


def test_runbook_block_documented():
    text = _section_d()
    assert "JAMF APPLY RUNBOOK" in text, \
        "Apply runbook header marker is missing"
    assert "parallelism=1" in text, \
        "parallelism=1 constraint must be documented"
    assert "jamfpro_load_balancer_lock" in text, \
        "jamfpro_load_balancer_lock = true constraint must be documented"


def test_all_12_resources_documented():
    text = _section_d()
    missing = [r for r in JAMF_RESOURCES if r not in text]
    assert not missing, f"Missing JAMF resources from SECTION D: {missing}"


def test_v2_smart_group_default():
    text = _section_d()
    assert "jamfpro_smart_computer_group_v2" in text, \
        "_v2 smart group resource must be documented"
    # The prompt must explicitly steer the model away from the v1 form.
    lower = text.lower()
    warns_v1 = ("legacy" in lower) or ("never use" in lower) or ("always use" in lower and "_v2" in text)
    assert warns_v1, \
        "Prompt must instruct ALWAYS use _v2 / warn against the legacy v1 smart group resource"


def test_unsupported_features_listed():
    text = _section_d()
    lower = text.lower()
    assert ("mdm lock" in lower) or ("live mdm commands" in lower), \
        "Unsupported MDM-command capability must be called out in SECTION D"
    assert "# NOTE" in text, \
        "NOTE-comment punt pattern must be referenced for unsupported capabilities"


def test_intent_parser_has_jamf_disambiguators():
    assert "jamfpro_policy" in INTENT_PARSER_SYSTEM_PROMPT, \
        "INTENT_PARSER_SYSTEM_PROMPT must allow jamfpro_policy as a valid resource_type"
    assert "jamfpro_smart_computer_group_v2" in INTENT_PARSER_SYSTEM_PROMPT, \
        "INTENT_PARSER_SYSTEM_PROMPT must include jamfpro_smart_computer_group_v2 disambiguator"


def test_validator_has_jamf_section():
    assert "JAMF" in VALIDATOR_SYSTEM_PROMPT, \
        "VALIDATOR_SYSTEM_PROMPT must mention JAMF in some checks section"
    assert "load_balancer_lock" in VALIDATOR_SYSTEM_PROMPT, \
        "VALIDATOR must check for the jamfpro_load_balancer_lock provider attr"


def test_no_em_dashes_in_jamf_prose():
    text = _section_d()
    assert chr(0x2014) not in text, \
        "Em-dash (\\u2014) characters are banned in SECTION D prose; use commas, semicolons, or rewrite"


_TESTS = [
    test_section_d_present,
    test_provider_pinned,
    test_yohan460_explicitly_rejected,
    test_runbook_block_documented,
    test_all_12_resources_documented,
    test_v2_smart_group_default,
    test_unsupported_features_listed,
    test_intent_parser_has_jamf_disambiguators,
    test_validator_has_jamf_section,
    test_no_em_dashes_in_jamf_prose,
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
