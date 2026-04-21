#!/usr/bin/env python3
"""
QA test runner for Okta Terraform + Lambda Generator.
Runs test scenarios directly against the parser + generator and checks outputs
for known failure patterns: hallucinated attributes, AWS bleed into Okta-only
outputs, wrong event types, bad resource type selection, invalid schemas.
"""

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import anthropic
from dotenv import load_dotenv
load_dotenv()

from generator.parser import parse_intent, validate_intent
from generator.terraform_gen import generate_all, GenerationError


# ──────────────────────────────────────────────────────────────────────────────
# Test case definitions
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class TestCase:
    id: str
    prompt: str
    okta_types: list = field(default_factory=list)
    aws_types: list = field(default_factory=list)
    expected_resource_type: Optional[str] = None
    # strings that MUST appear in terraform_okta_hcl
    must_contain: list = field(default_factory=list)
    # strings that must NOT appear in terraform_okta_hcl
    must_not_contain_okta: list = field(default_factory=list)
    notes: str = ""


HALLUCINATED_REMOVE_ATTRS = [
    "remove_group_ids",
    "remove_assigned_group_ids",
    "remove_assigned_user_ids",
    "remove_user_ids",
    "unassign_group_ids",
]

FORBIDDEN_EVENT_HOOK_ATTRS = ['"events"', '"filters"', '"auth_type"']

TEST_CASES = [
    # ── okta_group ────────────────────────────────────────────────────────────
    TestCase("G01", "Create a group called Engineering",
             expected_resource_type="okta_group",
             must_contain=["okta_group"]),
    TestCase("G02", "Create a group for the HR department",
             expected_resource_type="okta_group"),
    TestCase("G03", "Create a contractors group with a description",
             expected_resource_type="okta_group"),
    TestCase("G04", "Create a group called Tableau Viewers",
             expected_resource_type="okta_group"),
    TestCase("G05", "Add a security group named SecOps team",
             expected_resource_type="okta_group"),

    # ── okta_group_rule (add-only — never remove) ─────────────────────────────
    TestCase("GR01", "Create a rule that adds users with department=Engineering to the Engineering group",
             expected_resource_type="okta_group_rule",
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS),
    TestCase("GR02", "Automatically add contractors to the Contractors group based on their job title",
             expected_resource_type="okta_group_rule",
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS),
    TestCase("GR03", "Create a group rule assigning US employees when their country attribute is US",
             expected_resource_type="okta_group_rule",
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS),
    TestCase("GR04", "Rule: add users to the Management group when their title contains Manager",
             expected_resource_type="okta_group_rule",
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS),
    TestCase("GR05", "Assign all sales department users to the Sales group automatically",
             expected_resource_type="okta_group_rule",
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS),

    # ── okta_event_hook — group membership scenarios (must use group.user_membership.add) ──
    TestCase("EH01",
             "When a user is added to the Tableau Creator group, remove them from Tableau Viewer and Tableau Explorer",
             expected_resource_type="okta_event_hook",
             must_contain=["group.user_membership.add"],
             must_not_contain_okta=["user.lifecycle.create", "user.lifecycle.update"]
             + HALLUCINATED_REMOVE_ATTRS),
    TestCase("EH02",
             "Whenever a user joins the Admin group, automatically remove them from the Read-Only group",
             expected_resource_type="okta_event_hook",
             must_contain=["group.user_membership.add"],
             must_not_contain_okta=["user.lifecycle.create", "user.lifecycle.update"]),
    TestCase("EH03",
             "Build a hook that fires any time a user is added to a Tableau role group",
             expected_resource_type="okta_event_hook",
             must_contain=["group.user_membership.add"]),
    TestCase("EH04",
             "Set up an event hook for when users are removed from the Admins group",
             expected_resource_type="okta_event_hook",
             must_contain=["group.user_membership.remove"]),
    TestCase("EH05",
             "Create a webhook that enforces mutual exclusivity between Premium and Free tier groups",
             expected_resource_type="okta_event_hook",
             must_contain=["group.user_membership.add"],
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS),

    # ── okta_event_hook — user lifecycle scenarios ────────────────────────────
    TestCase("EH06",
             "Create an event hook that fires when a user is deactivated",
             expected_resource_type="okta_event_hook",
             must_contain=["user.lifecycle.deactivate"],
             must_not_contain_okta=["user.lifecycle.update"]),
    TestCase("EH07",
             "Set up an event hook to call an endpoint when a new user is created in Okta",
             expected_resource_type="okta_event_hook",
             must_contain=["user.lifecycle.create"]),
    TestCase("EH08",
             "Trigger a webhook when an Okta user is offboarded or deactivated",
             expected_resource_type="okta_event_hook",
             must_contain=["user.lifecycle.deactivate"]),
    TestCase("EH09",
             "Create an event hook for user activation events",
             expected_resource_type="okta_event_hook",
             must_contain=["user.lifecycle.activate"]),
    TestCase("EH10",
             "Notify an external system when a user's Okta profile is updated",
             expected_resource_type="okta_event_hook",
             must_contain=["user.account.update_profile"]),

    # ── okta_app_saml ─────────────────────────────────────────────────────────
    TestCase("AS01", "Create a SAML 2.0 app for Salesforce",
             okta_types=["okta_app_saml"], expected_resource_type="okta_app_saml",
             must_contain=["okta_app_saml"]),
    TestCase("AS02", "Set up SAML SSO for Google Workspace",
             okta_types=["okta_app_saml"], expected_resource_type="okta_app_saml"),
    TestCase("AS03", "Create a SAML application for our internal HR portal",
             okta_types=["okta_app_saml"], expected_resource_type="okta_app_saml"),
    TestCase("AS04", "Configure SAML SSO for ServiceNow",
             okta_types=["okta_app_saml"], expected_resource_type="okta_app_saml"),
    TestCase("AS05", "Add a new SAML app integration for Box",
             okta_types=["okta_app_saml"], expected_resource_type="okta_app_saml"),

    # ── okta_app_oauth ────────────────────────────────────────────────────────
    TestCase("AO01", "Create an OAuth 2.0 app for our internal dashboard",
             okta_types=["okta_app_oauth"], expected_resource_type="okta_app_oauth",
             must_contain=["okta_app_oauth"]),
    TestCase("AO02", "Set up OIDC SSO for our React single-page app",
             okta_types=["okta_app_oauth"], expected_resource_type="okta_app_oauth"),
    TestCase("AO03", "Create a machine-to-machine OAuth client credentials app",
             okta_types=["okta_app_oauth"], expected_resource_type="okta_app_oauth"),

    # ── okta_auth_server ──────────────────────────────────────────────────────
    TestCase("AUTH01", "Create a custom authorization server for the payments API",
             expected_resource_type="okta_auth_server",
             must_contain=["okta_auth_server"]),
    TestCase("AUTH02", "Set up an auth server with custom scopes for our mobile app",
             expected_resource_type="okta_auth_server"),
    TestCase("AUTH03", "Create an authorization server with a custom claim that includes user roles",
             expected_resource_type="okta_auth_server"),

    # ── okta_auth_server_policy ───────────────────────────────────────────────
    TestCase("AP01", "Create an access policy on the payments authorization server",
             expected_resource_type="okta_auth_server_policy",
             must_contain=["okta_auth_server_policy"]),
    TestCase("AP02", "Add an auth server policy rule limiting token lifetime to 1 hour",
             expected_resource_type="okta_auth_server_policy"),

    # ── okta_factor ───────────────────────────────────────────────────────────
    TestCase("MFA01", "Enable Google Authenticator as an MFA factor for the org",
             expected_resource_type="okta_factor",
             must_contain=["okta_factor"],
             must_not_contain_okta=["okta_policy"]),
    TestCase("MFA02", "Enable Okta Verify push notifications MFA for the org",
             expected_resource_type="okta_factor",
             must_not_contain_okta=["okta_policy"]),

    # ── okta_network_zone ─────────────────────────────────────────────────────
    TestCase("NZ01", "Create an IP allowlist network zone for our office CIDR ranges",
             expected_resource_type="okta_network_zone",
             must_contain=["okta_network_zone"]),
    TestCase("NZ02", "Set up a network zone that blocks access from specified IP ranges",
             expected_resource_type="okta_network_zone"),

    # ── okta_brand ────────────────────────────────────────────────────────────
    TestCase("BR01", "Customize the Okta org branding with company colors and logo",
             expected_resource_type="okta_brand",
             must_contain=["okta_brand"]),

    # ── okta_email_customization ──────────────────────────────────────────────
    TestCase("EM01", "Customize the user activation email template",
             expected_resource_type="okta_email_customization",
             must_contain=["okta_email_customization"]),
    TestCase("EM02", "Create a custom forgot password email for our org",
             expected_resource_type="okta_email_customization"),

    # ── AWS mode (Both) — Lambda must be generated ────────────────────────────
    TestCase("AW01", "Create an event hook that fires when a user is deactivated",
             aws_types=["aws_lambda_function"],
             must_contain=["user.lifecycle.deactivate"],
             notes="output_mode=Both: lambda_python must not be empty"),
    TestCase("AW02", "Set up a scheduled Lambda that checks for inactive Okta users daily",
             aws_types=["aws_lambda_function", "aws_cloudwatch_event_rule"],
             notes="EventBridge rule must appear in terraform_lambda_hcl"),
    TestCase("AW03", "Create an event hook with a Lambda URL endpoint for group membership events",
             aws_types=["aws_lambda_function", "aws_lambda_function_url"],
             notes="Lambda URL must appear in terraform_lambda_hcl"),
    TestCase("AW04", "Build a Lambda that fires on user deactivation and sends an SNS notification",
             aws_types=["aws_lambda_function", "aws_sns_topic"],
             notes="SNS topic must appear in terraform_lambda_hcl"),

    # ── Okta-only mode — strict zero-AWS checks ───────────────────────────────
    TestCase("OO01", "Create a SAML app for Workday",
             notes="Okta-only: lambda/AWS fields must be empty strings"),
    TestCase("OO02", "Create a group rule for all EU employees based on country attribute",
             notes="Okta-only: no aws_ anywhere"),
    TestCase("OO03", "Set up an event hook for user deactivations",
             notes="Okta-only: var.webhook_endpoint not a Lambda URL"),
    TestCase("OO04", "Create an authorization server for our mobile API with custom scopes",
             notes="Okta-only: no aws_ references"),
    TestCase("OO05", "Enable Duo Security as an MFA factor",
             notes="Okta-only: no aws_ references"),

    # ── Edge / regression ─────────────────────────────────────────────────────
    TestCase("ED01",
             "When a user joins the Terminated group, remove them from all other groups",
             expected_resource_type="okta_event_hook",
             must_contain=["group.user_membership.add"],
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS),
    TestCase("ED02",
             "Create a rule that adds users to Creator role and removes them from Viewer role",
             expected_resource_type="okta_event_hook",
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS,
             notes="Must route to event_hook not group_rule; no hallucinated removal attr"),
    TestCase("ED03",
             "Create a group membership rule based on the department profile attribute",
             expected_resource_type="okta_group_rule",
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS),
    TestCase("ED04",
             "Build a hook that removes users from the Premium group when they downgrade",
             expected_resource_type="okta_event_hook",
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS),
    TestCase("ED05",
             "Enforce that users can only be in one of: Free, Pro, or Enterprise tier group",
             expected_resource_type="okta_event_hook",
             must_contain=["group.user_membership.add"],
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS),

    # ── optional_tf collision tests (Both mode) ───────────────────────────────
    TestCase("OPT01",
             "When a user is removed from the Contractors group, deactivate their account. Also run a daily Lambda sweep for contractors whose end date has passed.",
             aws_types=["aws_lambda_function", "aws_cloudwatch_event_rule"],
             must_contain=["group.user_membership.remove"],
             notes="optional_tf must not redefine aws_lambda_function or aws_iam_role"),
    TestCase("OPT02",
             "Fire an event hook when a user is added to the Terminated group and send an SNS alert to the security team",
             aws_types=["aws_lambda_function", "aws_sns_topic"],
             must_contain=["group.user_membership.add"],
             notes="optional_tf must not redefine Lambda or use IAM policy name 'handler'"),
    TestCase("OPT03",
             "Create an event hook for user deactivation that calls a Lambda. Add a CloudWatch alarm on Lambda errors.",
             aws_types=["aws_lambda_function"],
             must_contain=["user.lifecycle.deactivate"],
             notes="optional_tf CloudWatch alarm must reference aws_lambda_function.handler, not redeclare it"),
    TestCase("OPT04",
             "Build a daily Lambda sweep that deactivates Okta users inactive for 90 days and sends an SNS notification",
             aws_types=["aws_lambda_function", "aws_cloudwatch_event_rule", "aws_sns_topic"],
             notes="optional_tf must not add a second aws_lambda_function resource"),
    TestCase("OPT05",
             "Set up an event hook for new user creation that triggers Lambda and also publishes to SNS for audit logging",
             aws_types=["aws_lambda_function", "aws_sns_topic"],
             must_contain=["user.lifecycle.create"],
             notes="SNS resources in optional_tf must not redefine Lambda or duplicate IAM policy"),
]


# ──────────────────────────────────────────────────────────────────────────────
# Check functions
# ──────────────────────────────────────────────────────────────────────────────

def run_checks(tc: TestCase, intent: dict, outputs: dict) -> list:
    """Returns list of (passed: bool, message: str)."""
    issues = []
    okta_hcl    = outputs.get("terraform_okta_hcl", "")
    lambda_hcl  = outputs.get("terraform_lambda_hcl", "")
    lambda_py   = outputs.get("lambda_python", "")
    lambda_req  = outputs.get("lambda_requirements", "")
    optional_tf = outputs.get("optional_tf", "") or ""
    output_mode = intent.get("output_mode", "Both")

    # ── 1. Okta-only: all AWS fields must be empty ─────────────────────────
    if output_mode == "Okta Terraform only":
        if lambda_hcl.strip():
            issues.append("terraform_lambda_hcl not empty in Okta-only mode")
        if lambda_py.strip():
            issues.append("lambda_python not empty in Okta-only mode")
        if lambda_req.strip():
            issues.append("lambda_requirements not empty in Okta-only mode")
        # No aws_ resource references in okta HCL
        aws_refs = [l.strip() for l in okta_hcl.splitlines() if re.search(r'\baws_\w+', l)]
        if aws_refs:
            issues.append(f"aws_ reference in terraform_okta_hcl: {aws_refs[:2]}")
        # No aws_ resource/data blocks in optional_tf
        if re.search(r'resource\s+"aws_|data\s+"aws_', optional_tf):
            issues.append("AWS resource/data block in optional_tf in Okta-only mode")
        # No actual aws_ TF resource references (not just the word in descriptions)
        aws_resource_refs = [l.strip() for l in okta_hcl.splitlines()
                             if re.search(r'resource\s+"aws_|data\s+"aws_|aws_lambda_function\.|aws_iam_role\.', l)]
        if aws_resource_refs:
            issues.append(f"AWS TF resource reference in terraform_okta_hcl: {aws_resource_refs[:2]}")

    # ── 2. Hallucinated group rule removal attributes ──────────────────────
    for attr in HALLUCINATED_REMOVE_ATTRS:
        if attr in okta_hcl:
            issues.append(f"Hallucinated attribute '{attr}' in okta HCL")

    # ── 3. Forbidden event hook attribute names ────────────────────────────
    if "okta_event_hook" in okta_hcl:
        for f in FORBIDDEN_EVENT_HOOK_ATTRS:
            if f in okta_hcl:
                issues.append(f"Forbidden event hook attribute {f}")
        if "channel" not in okta_hcl:
            issues.append("okta_event_hook missing 'channel' block")
        if "events_filter" not in okta_hcl:
            issues.append("okta_event_hook missing 'events_filter' block")

    # ── 4. Group membership scenarios must include group.user_membership.* ──
    is_group_scenario = any(kw in tc.prompt.lower() for kw in
                            ["added to", "remove from", "joins the", "mutual exclusiv",
                             "role transition", "only be in one"])
    if is_group_scenario and "okta_event_hook" in okta_hcl:
        if "group.user_membership" not in okta_hcl:
            issues.append("Group-membership scenario missing group.user_membership.* event — check event types")

    # ── 5. must_contain checks ─────────────────────────────────────────────
    for s in tc.must_contain:
        if s not in okta_hcl:
            issues.append(f"Expected '{s}' not found in terraform_okta_hcl")

    # ── 6. must_not_contain_okta checks ───────────────────────────────────
    for s in tc.must_not_contain_okta:
        if s in okta_hcl:
            issues.append(f"Forbidden string '{s}' found in terraform_okta_hcl")

    # ── 7. Both mode with AWS types: lambda must be non-empty ─────────────
    if output_mode == "Both" and tc.aws_types:
        if not lambda_py.strip():
            issues.append("output_mode=Both with AWS types but lambda_python is empty")
        if not lambda_hcl.strip():
            issues.append("output_mode=Both with AWS types but terraform_lambda_hcl is empty")

    # ── 8. Lambda handler signature ───────────────────────────────────────
    if lambda_py.strip() and "def handler(event, context):" not in lambda_py:
        issues.append("lambda_python missing 'def handler(event, context):' signature")

    # ── 9. No hardcoded secrets ────────────────────────────────────────────
    secret_patterns = [r'sk-ant-', r'AKIA[A-Z0-9]{16}', r'api_token\s*=\s*"[^"$]']
    for pat in secret_patterns:
        for hcl in [okta_hcl, lambda_hcl]:
            if re.search(pat, hcl):
                issues.append(f"Possible hardcoded secret (pattern: {pat})")

    # ── 10. Expected resource type ─────────────────────────────────────────
    if tc.expected_resource_type:
        actual = intent.get("resource_type", "")
        if actual != tc.expected_resource_type:
            issues.append(f"Parser chose '{actual}', expected '{tc.expected_resource_type}'")

    # ── 11. optional_tf must not redefine Lambda/IAM already in lambda_hcl ──
    if optional_tf.strip() and lambda_hcl.strip():
        if re.search(r'resource\s+"aws_lambda_function"', optional_tf):
            issues.append(
                "optional_tf redefines aws_lambda_function — add supplemental resources only, "
                "reference aws_lambda_function.handler instead"
            )
        if re.search(r'resource\s+"aws_iam_role"\s+"', optional_tf):
            issues.append(
                "optional_tf redefines aws_iam_role — reference aws_iam_role.handler.id instead"
            )
        if re.search(r'resource\s+"aws_iam_role_policy"\s+"handler"', optional_tf):
            issues.append(
                "optional_tf uses aws_iam_role_policy name 'handler' which conflicts with "
                "the existing policy in terraform_lambda_hcl — use a unique name"
            )

    return issues


# ──────────────────────────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────────────────────────

def build_intent(tc: TestCase, client, model: str) -> dict:
    intent = parse_intent(tc.prompt, client, model=model, resource_type_hints=tc.okta_types)
    if tc.okta_types:
        intent["resource_types"] = tc.okta_types
    if tc.aws_types:
        intent["aws_resource_types"] = tc.aws_types
    intent["output_mode"] = "Both" if tc.aws_types else "Okta Terraform only"
    intent["answers"] = {}
    intent["provider_version"] = "~> 4.0"
    return intent


def run_test(tc: TestCase, client, model: str) -> dict:
    start = time.time()
    try:
        intent = build_intent(tc, client, model)
        val_errors = validate_intent(intent)
        if val_errors:
            return {
                "id": tc.id, "status": "FAIL",
                "issues": [f"Intent validation: {e}" for e in val_errors],
                "resource_type": intent.get("resource_type"),
                "output_mode": intent.get("output_mode"),
                "elapsed": round(time.time() - start, 1),
            }
        outputs = generate_all(intent, extra_instructions="", client=client, model=model)
        issues = run_checks(tc, intent, outputs)
        return {
            "id": tc.id,
            "prompt": tc.prompt,
            "status": "PASS" if not issues else "FAIL",
            "issues": issues,
            "resource_type": intent.get("resource_type"),
            "output_mode": intent.get("output_mode"),
            "elapsed": round(time.time() - start, 1),
        }
    except GenerationError as e:
        return {
            "id": tc.id, "status": "ERROR",
            "issues": [f"GenerationError: {e}"],
            "elapsed": round(time.time() - start, 1),
        }
    except Exception as e:
        return {
            "id": tc.id, "status": "ERROR",
            "issues": [f"{type(e).__name__}: {e}"],
            "elapsed": round(time.time() - start, 1),
        }


def _read_api_key() -> str:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if key:
        return key
    # try .streamlit/secrets.toml
    try:
        path = os.path.join(os.path.dirname(__file__), ".streamlit", "secrets.toml")
        txt = open(path).read()
        m = re.search(r'ANTHROPIC_API_KEY\s*=\s*"([^"]+)"', txt)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def main():
    filter_ids = set(a.upper() for a in sys.argv[1:])
    cases = [tc for tc in TEST_CASES if not filter_ids or tc.id.upper() in filter_ids]

    api_key = _read_api_key()
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not found. Set it in the environment or .streamlit/secrets.toml")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

    print(f"QA runner — {len(cases)} tests — model: {model}")
    print("=" * 72)

    results = []
    passed = failed = errored = 0

    for i, tc in enumerate(cases, 1):
        label = f"[{i:02d}/{len(cases)}] {tc.id:<6} {tc.prompt[:55]:<55}"
        print(f"{label} ...", end="", flush=True)
        r = run_test(tc, client, model)
        results.append(r)
        elapsed = r.get("elapsed", 0)
        if r["status"] == "PASS":
            passed += 1
            print(f"\r{label} PASS  ({elapsed}s)")
        elif r["status"] == "FAIL":
            failed += 1
            print(f"\r{label} FAIL  ({elapsed}s)")
            for iss in r["issues"]:
                print(f"          -> {iss}")
        else:
            errored += 1
            print(f"\r{label} ERROR ({elapsed}s)")
            for iss in r["issues"]:
                print(f"          -> {iss}")

    print("\n" + "=" * 72)
    print(f"  PASSED : {passed}")
    print(f"  FAILED : {failed}")
    print(f"  ERRORS : {errored}")
    print(f"  TOTAL  : {len(cases)}")
    print("=" * 72)

    if failed or errored:
        print("\nFailing tests summary:")
        for r in results:
            if r["status"] != "PASS":
                rt = r.get("resource_type", "?")
                print(f"  {r['id']:<6} [{r['status']}]  parsed_as={rt}")
                for iss in r.get("issues", []):
                    print(f"         -> {iss}")

    report_path = os.path.join(os.path.dirname(__file__), "qa_report.json")
    with open(report_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nReport saved: {report_path}")

    sys.exit(0 if (failed + errored) == 0 else 1)


if __name__ == "__main__":
    main()
