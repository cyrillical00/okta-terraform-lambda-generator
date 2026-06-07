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
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import anthropic
from dotenv import load_dotenv
load_dotenv()

from pathlib import Path
from generator.parser import parse_intent, validate_intent
from generator.terraform_gen import generate_all, GenerationError

_OUTPUT_CACHE: dict = {}
CACHE_PATH = Path(__file__).parent / "qa_outputs_cache.json"

_USAGE_TOTALS = {
    "calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}

# Rates assume claude-haiku-4-5; adjust if ANTHROPIC_MODEL is overridden.
HAIKU_4_5_RATES_PER_M = {
    "input": 1.0,
    "output": 5.0,
    "cache_write": 1.25,
    "cache_read": 0.10,
}


def _wrap_client_for_usage_tracking(client):
    """Monkey-patch client.messages.create to accumulate usage totals."""
    original_create = client.messages.create

    def wrapped(*args, **kwargs):
        resp = original_create(*args, **kwargs)
        u = resp.usage
        _USAGE_TOTALS["calls"] += 1
        _USAGE_TOTALS["input_tokens"] += u.input_tokens
        _USAGE_TOTALS["output_tokens"] += u.output_tokens
        _USAGE_TOTALS["cache_creation_input_tokens"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        _USAGE_TOTALS["cache_read_input_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
        return resp

    client.messages.create = wrapped
    return client


def _print_usage_totals():
    t = _USAGE_TOTALS
    if t["calls"] == 0:
        return
    cost = (
        t["input_tokens"] * HAIKU_4_5_RATES_PER_M["input"]
        + t["output_tokens"] * HAIKU_4_5_RATES_PER_M["output"]
        + t["cache_creation_input_tokens"] * HAIKU_4_5_RATES_PER_M["cache_write"]
        + t["cache_read_input_tokens"] * HAIKU_4_5_RATES_PER_M["cache_read"]
    ) / 1_000_000
    cached_total = t["cache_creation_input_tokens"] + t["cache_read_input_tokens"]
    cache_hit_pct = (
        100.0 * t["cache_read_input_tokens"] / cached_total
        if cached_total else 0.0
    )
    print()
    print(f"  API calls            : {t['calls']:,}")
    print(f"  Input (uncached)     : {t['input_tokens']:>10,} tokens")
    print(f"  Output               : {t['output_tokens']:>10,} tokens")
    print(f"  Cache writes         : {t['cache_creation_input_tokens']:>10,} tokens")
    print(f"  Cache reads          : {t['cache_read_input_tokens']:>10,} tokens  ({cache_hit_pct:.1f}% hit on cached prefix)")
    print(f"  Estimated cost       : ${cost:.3f}  (Haiku 4.5: $1/$5/$1.25/$0.10 per M tokens)")


# ──────────────────────────────────────────────────────────────────────────────
# Test case definitions
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class TestCase:
    id: str
    prompt: str
    okta_types: list = field(default_factory=list)
    aws_types: list = field(default_factory=list)
    gcp_types: list = field(default_factory=list)
    jamf_types: list = field(default_factory=list)
    expected_resource_type: Optional[str] = None
    # strings that MUST appear in terraform_okta_hcl
    must_contain: list = field(default_factory=list)
    # strings that must NOT appear in terraform_okta_hcl
    must_not_contain_okta: list = field(default_factory=list)
    # strings that MUST appear in terraform_gcp_hcl (GCP/Okta+GCP modes)
    must_contain_gcp: list = field(default_factory=list)
    # strings that must NOT appear in terraform_gcp_hcl
    must_not_contain_gcp: list = field(default_factory=list)
    # strings that MUST appear in terraform_jamf_hcl (JAMF/Okta+JAMF modes)
    must_contain_jamf: list = field(default_factory=list)
    # strings that must NOT appear in terraform_jamf_hcl
    must_not_contain_jamf: list = field(default_factory=list)
    # Fleet GitOps YAML test fields (Fleet GitOps only / Okta + Fleet GitOps modes).
    # When fleet_types is set, build_intent routes the test to the GitOps YAML
    # output path and run_checks reads fleet_gitops_yaml.
    fleet_types: list = field(default_factory=list)
    must_contain_fleet: list = field(default_factory=list)
    must_not_contain_fleet: list = field(default_factory=list)
    # Fleet Terraform HCL test fields (Fleet TF only / Okta + Fleet TF modes).
    # When fleet_tf_types is set, build_intent routes the test to the Terraform
    # output path via the experimental l-teles/fleetdm provider; run_checks
    # reads terraform_fleet_hcl and composes with tf_validate.run_terraform.
    fleet_tf_types: list = field(default_factory=list)
    must_contain_fleet_tf: list = field(default_factory=list)
    must_not_contain_fleet_tf: list = field(default_factory=list)
    # Snowflake Terraform HCL test fields (Snowflake only / Okta + Snowflake).
    # When snowflake_types is set, build_intent routes the test to the
    # Snowflake output path via snowflakedb/snowflake ~> 2.0; run_checks
    # reads terraform_snowflake_hcl and composes with tf_validate.run_terraform.
    snowflake_types: list = field(default_factory=list)
    must_contain_snowflake: list = field(default_factory=list)
    must_not_contain_snowflake: list = field(default_factory=list)
    # Kandji (Iru) Terraform HCL test fields (Kandji only / Okta + Kandji).
    # When kandji_types is set, build_intent routes the test to the Kandji
    # output path via MScottBlake/iru ~> 0.0; run_checks reads
    # terraform_kandji_hcl and composes with tf_validate.run_terraform.
    kandji_types: list = field(default_factory=list)
    must_contain_kandji: list = field(default_factory=list)
    must_not_contain_kandji: list = field(default_factory=list)
    # Lumos Terraform HCL test fields (Lumos only / Okta + Lumos).
    # When lumos_types is set, build_intent routes the test to the Lumos
    # output path via teamlumos/lumos ~> 0.10; run_checks reads
    # terraform_lumos_hcl and composes with tf_validate.run_terraform.
    lumos_types: list = field(default_factory=list)
    must_contain_lumos: list = field(default_factory=list)
    must_not_contain_lumos: list = field(default_factory=list)
    # Multi-object reliability check: {resource_type: required_min_count} across
    # all HCL keys. Asserts the generator emitted N distinct `resource "X" "label"`
    # blocks of each named type. Closes the JF10/COMP02 class of failure where
    # the LLM emits one block representing multiple instances mashed together.
    must_contain_count: dict = field(default_factory=dict)
    notes: str = ""


HALLUCINATED_REMOVE_ATTRS = [
    "remove_group_ids",
    "remove_assigned_group_ids",
    "remove_assigned_user_ids",
    "remove_user_ids",
    "unassign_group_ids",
]

# Wrong attribute names / values that have shipped in real generations and would
# fail terraform validate against okta/okta ~> 4.0. Block in QA so the regression
# cannot return.
FORBIDDEN_GROUP_RULE_ATTRS = [
    # Match the bad attribute as an assignment, not as a substring of a variable
    # name like `group_ids_for_rule` which is legitimate.
    "group_ids =",
    "group_ids=",
    'type = "group_rule"',
    "urn:okta:expression:GroupRule",
    "urn:okta:expression:group:pred:expression",
]

FORBIDDEN_EVENT_HOOK_ATTRS = ['events_filter', '"filters"', '"auth_type"']

# Hallucinated provisioning block on okta_app_saml / okta_app_oauth.
# SCIM provisioning on app resources is NOT supported by the v4.x Okta provider —
# it is configured via the Okta Admin Console UI, not Terraform. Any provisioning {}
# block on a SAML or OAuth app will fail terraform validate.
FORBIDDEN_BRAND_ATTRS = ["logo", "primary_color", "secondary_color"]
FORBIDDEN_NETWORK_ZONE_ATTRS = ["ip_list", "allowed_ips", "blocked_ips", "cidr_ranges"]

# GCP — never emit. google_project_iam_policy is AUTHORITATIVE and overwrites
# the entire project IAM policy on apply (use google_project_iam_member instead).
# Cloud Functions Gen1 (no `2`) is deprecated; we ship Gen2 only.
FORBIDDEN_GCP_RESOURCES = [
    "google_project_iam_policy",
    "google_organization_iam_policy",
    "google_folder_iam_policy",
    "google_cloudfunctions_function",  # Gen1, deprecated
]

FORBIDDEN_APP_SCIM_ATTRS = [
    "provisioning {",
    "provisioning_type",
    "scim_enabled",
    "scim_url",
    "scim_settings",
    "scim_connector",
]

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
             must_contain=["expression_value", "group_assignments"],
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS + FORBIDDEN_GROUP_RULE_ATTRS),
    TestCase("GR02", "Automatically add contractors to the Contractors group based on their job title",
             expected_resource_type="okta_group_rule",
             must_contain=["expression_value", "group_assignments"],
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS + FORBIDDEN_GROUP_RULE_ATTRS),
    TestCase("GR03", "Create a group rule assigning US employees when their country attribute is US",
             expected_resource_type="okta_group_rule",
             must_contain=["expression_value", "group_assignments"],
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS + FORBIDDEN_GROUP_RULE_ATTRS),
    TestCase("GR04", "Rule: add users to the Management group when their title contains Manager",
             expected_resource_type="okta_group_rule",
             must_contain=["expression_value", "group_assignments"],
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS + FORBIDDEN_GROUP_RULE_ATTRS),
    TestCase("GR05", "Assign all sales department users to the Sales group automatically",
             expected_resource_type="okta_group_rule",
             must_contain=["expression_value", "group_assignments"],
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS + FORBIDDEN_GROUP_RULE_ATTRS),

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
    TestCase("AS06", "Create a SAML app called HR Portal for Workday with SCIM provisioning",
             okta_types=["okta_app_saml"], expected_resource_type="okta_app_saml",
             must_contain=["okta_app_saml"]),

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
             expected_resource_type="okta_auth_server_policy_rule",
             must_contain=["okta_auth_server_policy_rule"]),

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
             okta_types=["okta_event_hook"],
             aws_types=["aws_lambda_function"],
             must_contain=["user.lifecycle.deactivate"],
             notes="output_mode=Both: okta_event_hook + lambda_python both required"),
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

    # ── Compound multi-resource requests ─────────────────────────────────────
    TestCase("COMP01",
             "Create an OAuth 2.0 app for our developer portal and a custom auth server called Developer API with a read:data scope",
             expected_resource_type="okta_app_oauth",
             must_contain=["okta_app_oauth", "okta_auth_server", "okta_auth_server_scope"]),
    TestCase("COMP02",
             "Create a SAML app for Workday and assign three groups: HR, Finance, and Executives",
             expected_resource_type="okta_app_saml",
             must_contain=["okta_app_saml", "okta_group"],
             must_contain_count={"okta_app_group_assignment": 3}),
    TestCase("COMP03",
             "Create an auth server for our payments API with a payments:write scope and a role claim",
             expected_resource_type="okta_auth_server",
             must_contain=["okta_auth_server", "okta_auth_server_scope", "okta_auth_server_claim"]),

    # ── Complex multi-resource Okta workflows (added 2026-04-29) ──────────────
    TestCase("COMP09",
             "Set up a complete onboarding workflow: create groups for Engineering, Sales, and HR. Create group rules that auto-assign users to each group based on their department attribute. Create a SAML app for Workday with attribute statements for department and manager, and assign all three groups to it. Add an event hook that fires when a new user is created in Okta and notifies a Lambda for downstream provisioning.",
             okta_types=["okta_group", "okta_group_rule", "okta_app_saml", "okta_event_hook"],
             aws_types=["aws_lambda_function"],
             must_contain=["okta_group", "okta_group_rule", "okta_app_saml", "okta_event_hook",
                           "user.lifecycle.create", "okta_app_group_assignment"],
             must_contain_count={"okta_group": 3, "okta_group_rule": 3, "okta_app_group_assignment": 3},
             notes="Composite onboarding: 3 groups + 3 rules + SAML app + 3 assignments + event hook + Lambda"),
    TestCase("COMP10",
             "Set up a custom authorization server for our internal API. Define three scopes: read:data, write:data, and admin:data. Add two custom claims: a groups claim sourced from user.groups and a role claim sourced from user.profile.role. Create an access policy that allows read:data to all authenticated users but restricts write:data and admin:data to users in the API-Admins group. Create two OAuth apps: a public mobile app that requests only read:data and a confidential web app that requests all three scopes.",
             okta_types=["okta_auth_server", "okta_auth_server_scope", "okta_auth_server_claim",
                         "okta_auth_server_policy", "okta_auth_server_policy_rule", "okta_app_oauth"],
             must_contain=["okta_auth_server", "okta_auth_server_scope", "okta_auth_server_claim",
                           "okta_auth_server_policy", "okta_app_oauth", "read:data", "write:data", "admin:data"],
             must_contain_count={"okta_auth_server_scope": 3, "okta_auth_server_claim": 2, "okta_app_oauth": 2},
             notes="Zero-trust API access: full OAuth machinery (auth server + 3 scopes + 2 claims + policy + rule + 2 apps)"),
    TestCase("COMP11",
             "Build an offboarding pipeline: create a Terminated group; a group rule that adds users with employmentStatus equal to Terminated to that group; an event hook that fires on the user being added to the Terminated group; and a Lambda that calls the Okta API to deactivate the user, sends an SNS alert to the security team, and revokes their active sessions. Also enable Okta Verify push notifications as an MFA factor for the org.",
             okta_types=["okta_group", "okta_group_rule", "okta_event_hook", "okta_factor"],
             aws_types=["aws_lambda_function", "aws_sns_topic"],
             must_contain=["okta_group", "okta_group_rule", "okta_event_hook", "okta_factor",
                           "group.user_membership.add"],
             notes="Offboarding pipeline: group + rule + event hook on group.user_membership.add + Lambda + SNS + Okta Verify factor"),

    # ── optional_tf collision tests (Both mode) ───────────────────────────────
    TestCase("OPT01",
             "When a user is removed from the Contractors group, deactivate their account. Also run a daily Lambda sweep for contractors whose end date has passed.",
             okta_types=["okta_event_hook"],
             aws_types=["aws_lambda_function", "aws_cloudwatch_event_rule"],
             must_contain=["group.user_membership.remove"],
             notes="optional_tf must not redefine aws_lambda_function or aws_iam_role"),
    TestCase("OPT02",
             "Fire an event hook when a user is added to the Terminated group and send an SNS alert to the security team",
             okta_types=["okta_event_hook"],
             aws_types=["aws_lambda_function", "aws_sns_topic"],
             must_contain=["group.user_membership.add"],
             notes="optional_tf must not redefine Lambda or use IAM policy name 'handler'"),
    TestCase("OPT03",
             "Create an event hook for user deactivation that calls a Lambda. Add a CloudWatch alarm on Lambda errors.",
             okta_types=["okta_event_hook"],
             aws_types=["aws_lambda_function"],
             must_contain=["user.lifecycle.deactivate"],
             notes="optional_tf CloudWatch alarm must reference aws_lambda_function.handler, not redeclare it"),
    TestCase("OPT04",
             "Build a daily Lambda sweep that deactivates Okta users inactive for 90 days and sends an SNS notification",
             aws_types=["aws_lambda_function", "aws_cloudwatch_event_rule", "aws_sns_topic"],
             notes="optional_tf must not add a second aws_lambda_function resource"),
    TestCase("OPT05",
             "Set up an event hook for new user creation that triggers Lambda and also publishes to SNS for audit logging",
             okta_types=["okta_event_hook"],
             aws_types=["aws_lambda_function", "aws_sns_topic"],
             must_contain=["user.lifecycle.create"],
             notes="SNS resources in optional_tf must not redefine Lambda or duplicate IAM policy"),

    # ── okta_app_saml attribute statements — must be inline, not separate resource ──
    TestCase("SA01",
             "Create a SAML 2.0 app for Workday with an attribute statement mapping the user's role",
             okta_types=["okta_app_saml"],
             expected_resource_type="okta_app_saml",
             must_contain=["attribute_statements"],
             must_not_contain_okta=["okta_app_saml_attribute_statements"],
             notes="Attribute statements must be inline blocks, not a separate resource"),
    TestCase("SA02",
             "Create a SAML app for Salesforce and assign three groups: Sales, Sales Managers, and Sales Ops. Sales Managers get a role attribute statement.",
             okta_types=["okta_app_saml"],
             expected_resource_type="okta_app_saml",
             must_contain=["okta_app_group_assignment", "attribute_statements"],
             must_not_contain_okta=["okta_app_saml_attribute_statements"],
             must_contain_count={"okta_app_group_assignment": 3},
             notes="Group assignments via okta_app_group_assignment; attribute statements inline in okta_app_saml"),
    TestCase("SA03",
             "Set up a SAML 2.0 app for ServiceNow. Assign HR Full Access, HR Read Only, and Payroll Admins groups. HR Full Access and Payroll Admins need a role SAML attribute.",
             okta_types=["okta_app_saml"],
             expected_resource_type="okta_app_saml",
             must_contain=["attribute_statements", "okta_app_group_assignment"],
             must_not_contain_okta=["okta_app_saml_attribute_statements"],
             notes="Regression for the hallucinated okta_app_saml_attribute_statements resource"),

    # ── okta_app_oauth schema validation ──────────────────────────────────────
    TestCase("OA01",
             "Create an OAuth OIDC app for our internal React dashboard (single-page app)",
             okta_types=["okta_app_oauth"], expected_resource_type="okta_app_oauth",
             must_contain=["okta_app_oauth", "grant_types", "redirect_uris"],
             must_not_contain_okta=["client_id_scheme", "app_type"]),
    TestCase("OA02",
             "Set up a machine-to-machine OAuth client credentials app for our backend service",
             okta_types=["okta_app_oauth"], expected_resource_type="okta_app_oauth",
             must_contain=["grant_types"],
             must_not_contain_okta=["client_credentials {"]),
    TestCase("OA03",
             "Create an OAuth native mobile app with PKCE for iOS and Android",
             okta_types=["okta_app_oauth"], expected_resource_type="okta_app_oauth",
             must_contain=["grant_types", "redirect_uris"],
             must_not_contain_okta=["app_type"]),

    # ── okta_auth_server schema validation ────────────────────────────────────
    TestCase("AUTH04",
             "Create a custom authorization server for the payments API with a custom role claim",
             expected_resource_type="okta_auth_server",
             must_contain=["okta_auth_server", "audiences", "issuer_mode"]),
    TestCase("AUTH05",
             "Add an auth server policy that restricts token lifetime to 1 hour for the payments auth server",
             expected_resource_type="okta_auth_server_policy",
             must_contain=["okta_auth_server_policy", "priority"]),

    # ── okta_user_profile_mapping (intent label; emitted as okta_profile_mapping) ─
    TestCase("PM01", "Map the department attribute from the Workday app to the Okta user profile",
             expected_resource_type="okta_user_profile_mapping",
             must_contain=["okta_profile_mapping"]),
    TestCase("PM02", "Sync the user role attribute from Salesforce to the Okta Universal Directory",
             expected_resource_type="okta_user_profile_mapping"),
    TestCase("PM03", "Create a profile mapping that pushes the manager field from Okta to the HR portal app",
             expected_resource_type="okta_user_profile_mapping",
             must_contain=["okta_profile_mapping"]),
    TestCase("PM04", "Map custom department and costCenter attributes from our HRIS app to Okta user profiles",
             expected_resource_type="okta_user_profile_mapping"),
    TestCase("PM05", "Set up attribute mapping so the user's job title in Okta stays in sync with the HCM system",
             expected_resource_type="okta_user_profile_mapping"),

    # ── okta_auth_server_scope standalone ────────────────────────────────────
    TestCase("SC01", "Add a read:invoices scope to the payments authorization server",
             expected_resource_type="okta_auth_server_scope",
             must_contain=["okta_auth_server_scope"]),
    TestCase("SC02", "Create two scopes on the developer API auth server: read:data and write:data",
             expected_resource_type="okta_auth_server_scope",
             must_contain=["okta_auth_server_scope"],
             must_contain_count={"okta_auth_server_scope": 2}),
    TestCase("SC03", "Add a default openid scope to the mobile auth server",
             expected_resource_type="okta_auth_server_scope"),

    # ── okta_auth_server_claim standalone ────────────────────────────────────
    TestCase("CL01", "Add a groups claim to the payments auth server that includes the user's Okta groups",
             expected_resource_type="okta_auth_server_claim",
             must_contain=["okta_auth_server_claim"]),
    TestCase("CL02", "Create a custom role claim on the developer API auth server using a user profile expression",
             expected_resource_type="okta_auth_server_claim",
             must_contain=["okta_auth_server_claim", "claim_type"]),
    TestCase("CL03", "Add a department claim to the identity token on our internal auth server",
             expected_resource_type="okta_auth_server_claim"),

    # ── okta_network_zone dynamic ─────────────────────────────────────────────
    TestCase("NZD01", "Create a dynamic network zone that restricts access to users in the United States and Canada",
             expected_resource_type="okta_network_zone",
             must_contain=["okta_network_zone", "DYNAMIC"]),
    TestCase("NZD02", "Block access from ASNs associated with known VPN providers",
             expected_resource_type="okta_network_zone",
             must_contain=["okta_network_zone"]),
    TestCase("NZD03", "Create a geo-based network zone allowing only EU countries",
             expected_resource_type="okta_network_zone",
             must_contain=["okta_network_zone"]),

    # ── okta_email_customization additional templates ─────────────────────────
    TestCase("EMX01", "Customize the password changed notification email for our org",
             expected_resource_type="okta_email_customization",
             must_contain=["okta_email_customization", "PasswordChanged"]),
    TestCase("EMX02", "Create a custom email challenge template with our brand colors and logo link",
             expected_resource_type="okta_email_customization",
             must_contain=["okta_email_customization"]),
    TestCase("EMX03", "Customize the AD forgot password email template",
             expected_resource_type="okta_email_customization",
             must_contain=["okta_email_customization"]),
    TestCase("EMX04", "Write a custom account locked email template that includes our support contact",
             expected_resource_type="okta_email_customization",
             must_contain=["okta_email_customization"]),

    # ── okta_factor additional types ──────────────────────────────────────────
    TestCase("MFA03", "Enable Duo Security as a supported MFA factor for the org",
             expected_resource_type="okta_factor",
             must_contain=["okta_factor", "duo"],
             must_not_contain_okta=["okta_policy"]),
    TestCase("MFA04", "Enable FIDO2 WebAuthn as an MFA factor",
             expected_resource_type="okta_factor",
             must_contain=["okta_factor"],
             must_not_contain_okta=["okta_policy"]),
    TestCase("MFA05", "Enable YubiKey OTP as an MFA enrollment option for the org",
             expected_resource_type="okta_factor",
             must_contain=["okta_factor"],
             must_not_contain_okta=["okta_policy"]),

    # ── okta_event_hook additional scenarios ──────────────────────────────────
    TestCase("EHX01", "Create a hook that fires when a user's Okta profile attributes are updated",
             expected_resource_type="okta_event_hook",
             must_contain=["user.account.update_profile"],
             must_not_contain_okta=["user.lifecycle.create", "user.lifecycle.update"]),
    TestCase("EHX02", "Set up a webhook that triggers when a user changes their password",
             expected_resource_type="okta_event_hook",
             must_contain=["user.account.update_password"]),
    TestCase("EHX03",
             "Enforce that a user can only be in one Tableau role group at a time: Creator, Explorer, or Viewer",
             expected_resource_type="okta_event_hook",
             must_contain=["group.user_membership.add"],
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS),
    TestCase("EHX04",
             "Fire an event hook when a user is added to the Contractors group and notify an external HR system",
             expected_resource_type="okta_event_hook",
             must_contain=["group.user_membership.add"]),
    TestCase("EHX05", "Create a webhook triggered when a user account is activated in Okta",
             expected_resource_type="okta_event_hook",
             must_contain=["user.lifecycle.activate"]),

    # ── okta_group_rule additional scenarios ──────────────────────────────────
    TestCase("GRX01", "Create a group rule that adds users to the VP group when their title starts with VP",
             expected_resource_type="okta_group_rule",
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS),
    TestCase("GRX02", "Assign all full-time employees to the FTE group based on their employmentType attribute",
             expected_resource_type="okta_group_rule",
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS),
    TestCase("GRX03", "Rule: add users to the EMEA group when their region attribute equals EMEA",
             expected_resource_type="okta_group_rule",
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS),
    TestCase("GRX04", "Automatically assign premium tier users to the Premium group based on their subscriptionTier attribute",
             expected_resource_type="okta_group_rule",
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS),

    # ── okta_app_oauth additional schema validation ───────────────────────────
    TestCase("OAX01",
             "Create an OAuth PKCE app for a public mobile client with no client secret",
             okta_types=["okta_app_oauth"], expected_resource_type="okta_app_oauth",
             must_contain=["grant_types", "redirect_uris"],
             must_not_contain_okta=["client_credentials {"]),
    TestCase("OAX02",
             "Set up an OAuth web app with authorization code grant and post-logout redirect",
             okta_types=["okta_app_oauth"], expected_resource_type="okta_app_oauth",
             must_contain=["grant_types", "redirect_uris"],
             must_not_contain_okta=["client_id_scheme", "app_type"]),
    TestCase("OAX03",
             "Create an OAuth service account app using client credentials grant for a backend microservice",
             okta_types=["okta_app_oauth"], expected_resource_type="okta_app_oauth",
             must_contain=["grant_types"],
             # Match the bad attribute as an assignment (not as a substring of a
             # legitimate explanatory comment like "does not require redirect_uris").
             must_not_contain_okta=["redirect_uris =", "redirect_uris=", "client_credentials {"]),

    # ── AWS mode additional scenarios ─────────────────────────────────────────
    TestCase("AWX01",
             "Create an event hook for user deactivation with a REST API Gateway endpoint instead of a direct Lambda URL",
             okta_types=["okta_event_hook"],
             aws_types=["aws_lambda_function", "aws_api_gateway_rest_api"],
             must_contain=["user.lifecycle.deactivate"],
             notes="API Gateway resources must appear in terraform_lambda_hcl"),
    TestCase("AWX02",
             "Build a daily scheduled Lambda that reviews inactive Okta users and sends an SNS alert",
             aws_types=["aws_lambda_function", "aws_cloudwatch_event_rule", "aws_sns_topic"],
             notes="EventBridge + SNS must both appear in terraform_lambda_hcl"),
    TestCase("AWX03",
             "Set up a Lambda that fires when a user is added to the Offboarding group and sends an SNS notification to the security team",
             okta_types=["okta_event_hook"],
             aws_types=["aws_lambda_function", "aws_sns_topic"],
             must_contain=["group.user_membership.add"],
             notes="output_mode=Both: okta_event_hook + lambda_python + SNS topic all required"),
    TestCase("AWX04",
             "Create a scheduled Lambda that runs weekly to deprovision Okta users whose access end date has passed",
             aws_types=["aws_lambda_function", "aws_cloudwatch_event_rule"],
             notes="EventBridge schedule must appear; lambda must be non-empty"),

    # ── Compound multi-resource additional ────────────────────────────────────
    TestCase("COMP04",
             "Create an OIDC web app and restrict it to users in a US network zone",
             expected_resource_type="okta_app_oauth",
             must_contain=["okta_app_oauth", "okta_network_zone"]),
    TestCase("COMP05",
             "Create a Terminated group and an event hook that removes terminated users from all other groups when they join it",
             # Genuinely-compound prompt: both okta_group and okta_event_hook are defensible
             # primaries. Validate via must_contain (both resources present + correct event
             # type) instead of asserting which one the parser calls "primary".
             must_contain=["okta_group", "okta_event_hook", "group.user_membership.add"],
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS),
    TestCase("COMP06",
             "Create an authorization server for the mobile API with two scopes: read:profile and write:settings, and an access policy limiting token lifetime to 30 minutes",
             expected_resource_type="okta_auth_server",
             must_contain=["okta_auth_server", "okta_auth_server_scope", "okta_auth_server_policy"],
             must_contain_count={"okta_auth_server_scope": 2}),
    TestCase("COMP07",
             "Create a SAML app for Workday and map the costCenter and department attributes from Workday to the Okta user profile",
             expected_resource_type="okta_app_saml",
             must_contain=["okta_app_saml", "okta_profile_mapping"]),
    TestCase("COMP08",
             "Set up the complete onboarding email sequence: customize the activation email and the welcome email template",
             expected_resource_type="okta_email_customization",
             must_contain=["okta_email_customization"]),

    # ── Okta-only mode additional ─────────────────────────────────────────────
    TestCase("OOX01",
             "Create a custom authorization server for the internal API with a read scope",
             notes="Okta-only: no aws_ references in any output"),
    TestCase("OOX02",
             "Set up a network zone allowing only office IP ranges",
             notes="Okta-only: lambda fields must be empty"),
    TestCase("OOX03",
             "Create a user profile mapping from Workday to Okta",
             notes="Okta-only: no aws_ references"),
    TestCase("OOX04",
             "Customize the user activation and password changed email templates",
             notes="Okta-only: no Lambda or AWS in output"),

    # ── Edge / regression additional ──────────────────────────────────────────
    TestCase("EDX01",
             "Create a rule that moves users to the Archive group, but the rule should only add, not remove",
             expected_resource_type="okta_group_rule",
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS,
             notes="Regression: 'move' language must not produce hallucinated remove attrs"),
    TestCase("EDX02",
             "When a user transitions from the Free tier to the Pro tier group, remove them from Free",
             expected_resource_type="okta_event_hook",
             must_contain=["group.user_membership.add"],
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS,
             notes="Transition language must route to event_hook, not group_rule"),
    TestCase("EDX03",
             "Create a group rule that assigns users to the Beta Testers group when their betaAccess attribute is true",
             expected_resource_type="okta_group_rule",
             must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS),
    TestCase("EDX04",
             "Set up a SAML app for Greenhouse and make sure attribute statements for the hiring manager field are inline",
             okta_types=["okta_app_saml"],
             expected_resource_type="okta_app_saml",
             must_contain=["attribute_statements"],
             must_not_contain_okta=["okta_app_saml_attribute_statements"],
             notes="Regression: no hallucinated separate attribute resource"),

    # ── GCP module — Phase 1 verification ─────────────────────────────────────
    TestCase("GCP01",
             "Create a Cloud Function that responds to HTTP requests and returns a JSON status",
             gcp_types=["google_cloudfunctions2_function"],
             must_contain_gcp=[
                 'provider "google"',
                 'resource "google_cloudfunctions2_function" "handler"',
                 'resource "google_service_account" "handler"',
                 'runtime     = "python311"',
                 'entry_point = "main"',
             ],
             notes="Single-function HTTP trigger — exercises the standard Gen2 stack: SA + source bucket + function"),
    TestCase("GCP02",
             "Create a Pub/Sub topic called demo-events with a Cloud Function subscriber that logs each message",
             gcp_types=["google_cloudfunctions2_function", "google_pubsub_topic"],
             must_contain_gcp=[
                 'resource "google_pubsub_topic" "handler"',
                 'resource "google_cloudfunctions2_function" "handler"',
                 "event_trigger",
                 "google.cloud.pubsub.topic.v1.messagePublished",
             ],
             notes="Pub/Sub trigger — function must wire event_trigger to the topic"),
    TestCase("GCP03",
             "Deploy a Cloud Run service called internal-api running a custom container",
             gcp_types=["google_cloud_run_v2_service"],
             must_contain_gcp=[
                 'resource "google_cloud_run_v2_service"',
                 "template",
                 "containers",
                 'google_service_account.',
             ],
             notes="Cloud Run Gen2 service: must use the v2 resource and template/containers shape. Service-account reference is checked by substring (any whitespace, any resource name)."),
    TestCase("GCP04",
             "Create a daily scheduled Cloud Function that runs at 9 AM UTC and processes pending records",
             gcp_types=["google_cloudfunctions2_function", "google_cloud_scheduler_job"],
             must_contain_gcp=[
                 'resource "google_cloud_scheduler_job" "handler"',
                 'resource "google_cloudfunctions2_function" "handler"',
                 "http_target",
                 "oidc_token",
             ],
             notes="Scheduler + Function — scheduler must invoke the function via OIDC"),
    TestCase("GCP05",
             "Create an Okta event hook that fires on user deactivation and calls a GCP Cloud Function",
             okta_types=["okta_event_hook"],
             gcp_types=["google_cloudfunctions2_function"],
             expected_resource_type="okta_event_hook",
             must_contain=['resource "okta_event_hook"', "user.lifecycle.deactivate"],
             must_contain_gcp=[
                 'resource "google_cloudfunctions2_function" "handler"',
             ],
             notes="Okta + GCP composite — Okta event hook with channel.uri pointing at the Cloud Function URI, no AWS Lambda"),

    # ── Complex multi-resource GCP workflows (added 2026-04-29) ───────────────
    TestCase("GCPX01",
             "Create a Pub/Sub topic called orders that fans out to two Cloud Functions: one called order-processor that records the order to a database, and one called order-notifier that sends an email confirmation to the customer. Both functions must be triggered by the same topic.",
             gcp_types=["google_cloudfunctions2_function", "google_pubsub_topic"],
             must_contain_gcp=[
                 'resource "google_pubsub_topic"',
                 'resource "google_cloudfunctions2_function"',
                 "google.cloud.pubsub.topic.v1.messagePublished",
                 "event_trigger",
             ],
             must_not_contain_gcp=["google_cloudfunctions_function"],
             must_contain_count={"google_cloudfunctions2_function": 2},
             notes="Pub/Sub fan-out: 1 topic, 2 subscriber functions. Both functions must wire event_trigger to the same topic."),
    TestCase("GCPX02",
             "Create a Cloud Storage bucket called document-uploads. When a new object is finalized in the bucket, fire a Cloud Function called document-processor that reads the object, extracts metadata, and writes a JSON summary to a separate metadata bucket.",
             gcp_types=["google_cloudfunctions2_function", "google_storage_bucket"],
             must_contain_gcp=[
                 'resource "google_storage_bucket"',
                 'resource "google_cloudfunctions2_function"',
                 "google.cloud.storage.object.v1.finalized",
                 "event_trigger",
             ],
             must_not_contain_gcp=["google_cloudfunctions_function"],
             notes="GCS object-finalize trigger: bucket + Cloud Function that fires on new object upload."),
    TestCase("GCPX03",
             "Create a Cloud Function that reads an API key from Secret Manager at runtime and calls an external weather API to return the forecast for a given city. The function's service account must have only secretmanager.secretAccessor on that specific secret.",
             gcp_types=["google_cloudfunctions2_function", "google_secret_manager_secret"],
             must_contain_gcp=[
                 'resource "google_secret_manager_secret"',
                 'resource "google_cloudfunctions2_function"',
                 "roles/secretmanager.secretAccessor",
             ],
             must_not_contain_gcp=["google_project_iam_policy"],
             notes="Secret Manager + Cloud Function: SA reads secret at runtime via least-privilege IAM binding (member, not policy)."),
    TestCase("GCPX04",
             "Create a new GCP project called gemini-sandbox under my organization, create a service account called gemini-runner inside it, mint an API key for that project, enable the Gemini / Vertex AI API, and grant my user (oleg@example.com) the serviceAccountUser role on the gemini-runner SA so I can impersonate it locally.",
             gcp_types=["google_project", "google_service_account",
                        "google_apikeys_key", "google_project_service",
                        "google_service_account_iam_member"],
             must_contain_gcp=[
                 'resource "google_project"',
                 'resource "google_service_account"',
                 'resource "google_apikeys_key"',
                 "aiplatform.googleapis.com",
                 "roles/iam.serviceAccountUser",
             ],
             must_not_contain_gcp=["google_project_iam_policy",
                                   "google_organization_iam_policy",
                                   "google_service_account_iam_policy"],
             notes="Project provisioning + Vertex AI + API key + SA impersonation grant. "
                   "Apply requires org-admin perms (org_id, billing_account)."),

    # ── JAMF Pro (deploymenttheory/jamfpro ~> 0.37) ────────────────────────────
    TestCase("JF01",
             "Create a JAMF policy that installs the Slack package on enrollment.",
             jamf_types=["jamfpro_policy", "jamfpro_package"],
             must_contain_jamf=[
                 'resource "jamfpro_policy"',
                 "deploymenttheory/jamfpro",
                 "JAMF APPLY RUNBOOK",
                 "parallelism=1",
             ],
             must_not_contain_jamf=["yohan460/jamf"],
             notes="Core policy + package metadata. Binary uploads out-of-band."),

    TestCase("JF02",
             "Create a JAMF smart computer group for Macs running macOS Sonoma or later.",
             jamf_types=["jamfpro_smart_computer_group_v2"],
             must_contain_jamf=[
                 'resource "jamfpro_smart_computer_group_v2"',
                 "criteria",
             ],
             must_not_contain_jamf=[
                 'resource "jamfpro_smart_computer_group"',  # v1 legacy
             ],
             notes="Default to v2; flag v1 as legacy."),

    TestCase("JF03",
             "Create a JAMF script that clears caches, scoped to a recurring weekly trigger via a policy.",
             jamf_types=["jamfpro_script", "jamfpro_policy"],
             must_contain_jamf=[
                 'resource "jamfpro_script"',
                 'resource "jamfpro_policy"',
                 'script_contents',  # externalised either via file(...) or var.X
             ],
             notes="Script + policy with weekly trigger; script_contents externalised (file() OR var.X)."),

    TestCase("JF04",
             "Create a JAMF macOS configuration profile for our corporate Wi-Fi settings.",
             jamf_types=["jamfpro_macos_configuration_profile_plist_generator"],
             must_contain_jamf=[
                 'resource "jamfpro_macos_configuration_profile_plist_generator"',
             ],
             notes="Use _plist_generator for value-based config (not _plist for raw plist files)."),

    TestCase("JF05",
             "Restrict Spotify from running on managed Macs in JAMF.",
             jamf_types=["jamfpro_restricted_software"],
             must_contain_jamf=[
                 'resource "jamfpro_restricted_software"',
                 "Spotify",
                 "process_name",
             ],
             notes="Restricted software with kill_process behavior."),

    TestCase("JF06",
             "Create a JAMF static computer group called Test Devices.",
             jamf_types=["jamfpro_static_computer_group"],
             must_contain_jamf=[
                 'resource "jamfpro_static_computer_group"',
             ],
             notes="Static group with manual computer ID list."),

    TestCase("JF07",
             "Create a JAMF computer extension attribute that reports the last user login via a script.",
             jamf_types=["jamfpro_computer_extension_attribute"],
             must_contain_jamf=[
                 'resource "jamfpro_computer_extension_attribute"',
                 "input_type",
             ],
             notes="EA with input_type=Script for dynamic reporting."),

    TestCase("JF08",
             "Set up a JAMF DEP prestage enrollment for sales devices.",
             jamf_types=["jamfpro_computer_prestage_enrollment"],
             must_contain_jamf=[
                 'resource "jamfpro_computer_prestage_enrollment"',
             ],
             notes="DEP prestage with default skip_setup_items + auto_advance."),

    TestCase("JF09",
             "Upload a JAMF package metadata entry for Chrome.pkg.",
             jamf_types=["jamfpro_package"],
             must_contain_jamf=[
                 'resource "jamfpro_package"',
                 "Chrome",
             ],
             notes="Package metadata only; binary uploads out-of-band."),

    TestCase("JF10",
             "Create three JAMF smart computer groups: Engineering Macs, Sales Macs, and Marketing Macs.",
             jamf_types=["jamfpro_smart_computer_group_v2"],
             must_contain_jamf=[
                 'resource "jamfpro_smart_computer_group_v2"',
                 "Engineering Macs",
                 "Sales Macs",
                 "Marketing Macs",
             ],
             must_contain_count={"jamfpro_smart_computer_group_v2": 3},
             notes="Multi-object emission: 3 separate blocks (no for_each yet without multi-object phase)."),

    TestCase("JF11",
             "Create an Okta group called Engineering and a JAMF smart computer group with the same name filtering on the engineering department attribute.",
             okta_types=["okta_group"],
             jamf_types=["jamfpro_smart_computer_group_v2"],
             must_contain=["okta_group"],
             must_contain_jamf=[
                 'resource "jamfpro_smart_computer_group_v2"',
                 "Engineering",
             ],
             notes="Composite Okta + JAMF; cross-reference via var.* not direct resource ref."),

    TestCase("JF12",
             "Run an MDM lock command on all managed devices via Terraform.",
             jamf_types=["jamfpro_policy"],
             must_contain_jamf=[
                 "# NOTE",
             ],
             notes="Forbidden: live MDM commands not in any provider; emit NOTE pointing to JAMF console."),

    # ── Fleet GitOps YAML (Phase 13 Half A) ───────────────────────────────────
    TestCase("FG01",
             "Create a Fleet policy that checks if FileVault is enabled on Macs.",
             fleet_types=["fleet_policy"],
             must_contain_fleet=[
                 "policies:",
                 "FileVault",
                 "platform: darwin",
                 "query:",
             ],
             notes="Fleet GitOps YAML: single policy with darwin platform + osquery SQL."),
    TestCase("FG02",
             "Create a dynamic Fleet label for hosts on Arm64 architecture.",
             fleet_types=["fleet_label"],
             must_contain_fleet=[
                 "labels:",
                 "Arm64",
                 "label_membership_type: dynamic",
                 "query:",
             ],
             must_not_contain_fleet=["label_membership_type: manual"],
             notes="Dynamic label with osquery membership query."),
    TestCase("FG03",
             "Create a manual Fleet label called C-Suite with explicit host UUIDs.",
             fleet_types=["fleet_label"],
             must_contain_fleet=[
                 "labels:",
                 "C-Suite",
                 "label_membership_type: manual",
                 "hosts:",
             ],
             must_not_contain_fleet=["label_membership_type: dynamic"],
             notes="Manual label with explicit hosts list (no query)."),
    TestCase("FG04",
             "Create a Fleet saved query that lists installed Chrome extensions, running daily.",
             fleet_types=["fleet_query"],
             must_contain_fleet=[
                 "queries:",
                 "chrome_extensions",
                 "interval:",
                 "86400",
             ],
             notes="Saved query with 86400-second (daily) interval."),
    TestCase("FG05",
             "Push a corporate Wi-Fi configuration profile to all Macs in Fleet.",
             fleet_types=["fleet_configuration_profile"],
             must_contain_fleet=[
                 "controls:",
                 "apple_settings:",
                 "configuration_profiles:",
                 ".mobileconfig",
             ],
             notes="Apple configuration profile reference; expects path: or paths: form."),
    TestCase("FG06",
             "Deploy a Fleet script that clears DNS cache on macOS.",
             fleet_types=["fleet_script"],
             must_contain_fleet=[
                 "controls:",
                 "scripts:",
                 "path:",
                 ".sh",
             ],
             notes="Fleet script reference; external .sh file path."),
    TestCase("FG07",
             "Deploy Slack via Fleet using the fleet_maintained_apps catalog.",
             fleet_types=["fleet_software_package"],
             must_contain_fleet=[
                 "software:",
                 "fleet_maintained_apps:",
                 "slack",
                 "self_service:",
             ],
             notes="Fleet-maintained app deployment via slug."),
    TestCase("FG08",
             "Configure Fleet agent options to set distributed_interval to 30 seconds.",
             fleet_types=["fleet_agent_options"],
             must_contain_fleet=[
                 "agent_options:",
                 "config:",
                 "options:",
                 "distributed_interval: 30",
             ],
             notes="Agent options with explicit distributed_interval."),
    TestCase("FG09",
             "Set Fleet macOS update enforcement to require macOS 14.5 by 2026-05-24.",
             fleet_types=["fleet_team_settings"],
             must_contain_fleet=[
                 "controls:",
                 "macos_updates:",
                 "minimum_version:",
                 "14.5",
                 "deadline:",
             ],
             notes="macOS update enforcement under controls.macos_updates."),
    TestCase("FG10",
             "Create three Fleet policies: FileVault enabled, Gatekeeper enabled, and SIP enabled. All darwin platform.",
             fleet_types=["fleet_policy"],
             must_contain_fleet=[
                 "policies:",
                 "FileVault",
                 "Gatekeeper",
                 "SIP",
             ],
             notes="Multi-object: 3 distinct policy entries in the YAML."),
    TestCase("FG11",
             "Create a Fleet policy that runs only on hosts matching a dynamic 'Production' label.",
             fleet_types=["fleet_policy", "fleet_label"],
             must_contain_fleet=[
                 "policies:",
                 "labels:",
                 "Production",
                 "labels_include_any:",
             ],
             notes="Policy + label compound; policy is scoped via labels_include_any."),
    TestCase("FG12",
             "Create an Okta group called Fleet Admins AND a Fleet policy that runs on Macs in that group.",
             okta_types=["okta_group"],
             fleet_types=["fleet_policy"],
             must_contain=["okta_group"],
             must_contain_fleet=[
                 "policies:",
             ],
             notes="Composite Okta + Fleet GitOps: both terraform_okta_hcl and fleet_gitops_yaml populated."),

    # ── Fleet Terraform HCL (Phase 19a) ───────────────────────────────────────
    # FT01-FT12 mirror the FG01-FG12 prompts above but route through the
    # `Fleet TF only` / `Okta + Fleet TF` output modes via fleet_tf_types.
    # Assertions are grounded in the cached l-teles/fleetdm v0.5.4 BINARY
    # schema (terraform providers schema -json), NOT the bundled README, which
    # documents several attributes the binary does not accept. Forbidden
    # strings explicitly reject the pre-Phase-19a SECTION J vocabulary
    # (`url = `, `api_token = `, `fleetdm_query`, `fleetdm_team`) AND the
    # binary-incompatible vocabulary from the README itself (string `platform`,
    # `agent_options_json`, `fleet_maintained_app_slug`, `macos_updates` block,
    # `label_membership_type`, `path = ` on fleetdm_script).
    TestCase("FT01",
             "Create a Fleet policy via Terraform that checks if FileVault is enabled on Macs.",
             fleet_tf_types=["fleet_policy"],
             must_contain_fleet_tf=[
                 'resource "fleetdm_policy"',
                 "FileVault",
                 'platform',
                 '"darwin"',
                 "query",
                 'server_address = var.fleetdm_url',
                 'api_key        = var.fleetdm_api_key',
             ],
             must_not_contain_fleet_tf=[
                 'resource "fleetdm_team"',
                 'resource "fleetdm_query"',
                 'url       = var.fleet_url',
                 'api_token = var.fleet_api_token',
             ],
             notes="Single fleetdm_policy with darwin platform (binary needs list, NOT string) + osquery SQL."),
    TestCase("FT02",
             'Create a dynamic Fleet label via Terraform for hosts on Arm64 architecture. Name the label exactly "Arm64".',
             fleet_tf_types=["fleet_label"],
             must_contain_fleet_tf=[
                 'resource "fleetdm_label"',
                 "Arm64",
                 "query",
             ],
             must_not_contain_fleet_tf=[
                 'label_membership_type',
                 'hosts ',
                 'resource "fleetdm_team"',
                 'resource "fleetdm_query"',
                 'api_token = var.fleet_api_token',
             ],
             notes="Dynamic fleetdm_label with osquery membership query; binary has no label_membership_type or hosts attrs."),
    TestCase("FT03",
             "Create a Fleet label via Terraform called C-Suite that matches the executive roster by hardware_serial. The hardware serials are IR7M6ZGQJM and JMFWY8VZ09. The v0.5.4 binary only supports dynamic labels, so render this as a dynamic label whose query matches by hardware_serial IN (...).",
             fleet_tf_types=["fleet_label"],
             must_contain_fleet_tf=[
                 'resource "fleetdm_label"',
                 "C-Suite",
                 "query",
                 "IR7M6ZGQJM",
                 "hardware_serial",
             ],
             must_not_contain_fleet_tf=[
                 'label_membership_type',
                 'resource "fleetdm_team"',
                 'resource "fleetdm_query"',
             ],
             notes="Dynamic label workaround for manual-roster intent; binary does not expose manual labels."),
    TestCase("FT04",
             "Create a Fleet saved query via Terraform that lists installed Chrome extensions, running daily.",
             fleet_tf_types=["fleet_query"],
             must_contain_fleet_tf=[
                 'resource "fleetdm_report"',
                 "chrome_extensions",
                 "interval",
                 "86400",
                 'logging',
                 '"snapshot"',
             ],
             must_not_contain_fleet_tf=[
                 'resource "fleetdm_query"',
                 'resource "fleetdm_team"',
                 'url       = var.fleet_url',
             ],
             notes="fleetdm_report (canonical replacement for deprecated fleetdm_query) with 86400-second interval."),
    TestCase("FT05",
             "Push a corporate Wi-Fi configuration profile to all Macs in Fleet via Terraform. The profile XML payload is short and well-known; inline it as a heredoc string in profile_content (do NOT use file() as the attribute value; the regression workspace evaluates validate at parse time and cannot read external files). The attribute MUST be assigned a heredoc, not a function call.",
             fleet_tf_types=["fleet_configuration_profile"],
             must_contain_fleet_tf=[
                 'resource "fleetdm_configuration_profile"',
                 "# PREMIUM",
                 "profile_content",
                 "EOT",
             ],
             must_not_contain_fleet_tf=[
                 'resource "fleetdm_team"',
                 'resource "fleetdm_query"',
                 'api_token = var.fleet_api_token',
                 'path = "',
                 'mobileconfig =',
                 'profile_content = file(',
             ],
             notes="fleetdm_configuration_profile (Premium) with inline heredoc profile_content for validate-time correctness."),
    TestCase("FT06",
             "Deploy a Fleet script via Terraform that clears DNS cache on macOS. The script content is `sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder`. Inline a fleetdm_fleet \"default\" resource for the team_id. Inline the script body as a quoted string assigned to `content` (do NOT use file() as the attribute value; the regression workspace evaluates validate at parse time and cannot read external files).",
             fleet_tf_types=["fleet_script"],
             must_contain_fleet_tf=[
                 'resource "fleetdm_script"',
                 'resource "fleetdm_fleet"',
                 "team_id",
                 "name",
                 "content",
                 "dscacheutil",
             ],
             must_not_contain_fleet_tf=[
                 'resource "fleetdm_team"',
                 'resource "fleetdm_query"',
                 'path = "',
                 'content = file(',
             ],
             notes="fleetdm_script with binary-required team_id + inline content string (no path attribute on the binary)."),
    TestCase("FT07",
             "Deploy Slack via Fleet using the fleet-maintained apps catalog via Terraform. Use the numeric fleet_maintained_app_id form. Use 12 as a placeholder app id with a comment that the real id should be resolved via the Fleet UI.",
             fleet_tf_types=["fleet_software_package"],
             must_contain_fleet_tf=[
                 'resource "fleetdm_software_package"',
                 "# PREMIUM",
                 "fleet_maintained_app_id",
                 "self_service",
             ],
             must_not_contain_fleet_tf=[
                 'resource "fleetdm_team"',
                 'resource "fleetdm_query"',
                 'fleet_maintained_app_slug',
                 'categories =',
             ],
             notes="fleetdm_software_package (Premium) using binary-required fleet_maintained_app_id (number, not slug)."),
    TestCase("FT08",
             'Configure Fleet agent options via Terraform to set distributed_interval to 30 seconds. The org_name is "Example Corp".',
             fleet_tf_types=["fleet_agent_options"],
             must_contain_fleet_tf=[
                 'resource "fleetdm_configuration"',
                 "org_name",
                 "agent_options",
                 "distributed_interval",
                 "30",
             ],
             must_not_contain_fleet_tf=[
                 'resource "fleetdm_team"',
                 'resource "fleetdm_query"',
                 'agent_options_json',
                 'url       = var.fleet_url',
             ],
             notes="fleetdm_configuration with binary-required org_name + agent_options (string, NOT agent_options_json)."),
    TestCase("FT09",
             "Set up a Fleet team via Terraform called Engineering. The user wants macOS update enforcement to require macOS 14.5 by 2026-05-24, but the v0.5.4 binary does not expose a macos_updates block on fleetdm_fleet, so emit a fleetdm_fleet with just name + description and add a # NOTE comment explaining the limitation.",
             fleet_tf_types=["fleet_team_settings"],
             must_contain_fleet_tf=[
                 'resource "fleetdm_fleet"',
                 "Engineering",
                 "# NOTE",
                 "14.5",
             ],
             must_not_contain_fleet_tf=[
                 'resource "fleetdm_team"',
                 'resource "fleetdm_query"',
                 "macos_updates {",
                 "windows_updates {",
             ],
             notes="fleetdm_fleet without macos_updates block (binary does not accept it); NOTE preserves intent."),
    TestCase("FT10",
             "Create three Fleet policies via Terraform: FileVault enabled, Gatekeeper enabled, and SIP enabled. All darwin platform.",
             fleet_tf_types=["fleet_policy"],
             must_contain_fleet_tf=[
                 'resource "fleetdm_policy"',
                 "FileVault",
                 "Gatekeeper",
                 "SIP",
                 '"darwin"',
             ],
             must_not_contain_fleet_tf=[
                 'resource "fleetdm_team"',
                 'resource "fleetdm_query"',
             ],
             must_contain_count={"fleetdm_policy": 3},
             notes="Multi-object: 3 distinct fleetdm_policy blocks, all with platform as a list."),
    TestCase("FT11",
             "Create a Fleet policy via Terraform that runs only on hosts matching a dynamic 'Production' label.",
             fleet_tf_types=["fleet_policy", "fleet_label"],
             must_contain_fleet_tf=[
                 'resource "fleetdm_policy"',
                 'resource "fleetdm_label"',
                 "Production",
             ],
             must_not_contain_fleet_tf=[
                 'resource "fleetdm_team"',
                 'resource "fleetdm_query"',
                 'api_token = var.fleet_api_token',
                 'label_membership_type',
             ],
             notes="Policy + label compound; both resource types in one terraform_fleet_hcl."),
    TestCase("FT12",
             "Create an Okta group called Fleet Admins AND a Fleet policy via Terraform that runs on Macs in that group.",
             okta_types=["okta_group"],
             fleet_tf_types=["fleet_policy"],
             must_contain=["okta_group"],
             must_contain_fleet_tf=[
                 'resource "fleetdm_policy"',
                 'server_address = var.fleetdm_url',
             ],
             must_not_contain_fleet_tf=[
                 'resource "fleetdm_team"',
                 'resource "fleetdm_query"',
             ],
             notes="Composite Okta + Fleet TF: both terraform_okta_hcl and terraform_fleet_hcl populated."),

    # ── Snowflake Terraform via snowflakedb/snowflake ~> 2.0 (Phase 15) ───────
    TestCase("SF01",
             "Create a Snowflake warehouse called ETL_WH, MEDIUM size, auto-suspend 60 seconds.",
             snowflake_types=["snowflake_warehouse"],
             must_contain_snowflake=[
                 'resource "snowflake_warehouse"',
                 "ETL_WH",
                 'warehouse_size',
                 "MEDIUM",
                 "auto_suspend",
                 "60",
             ],
             notes="Snowflake warehouse with size + auto-suspend."),
    TestCase("SF02",
             "Create a Snowflake database called Analytics with three schemas: PUBLIC, RAW, and STAGING.",
             snowflake_types=["snowflake_database", "snowflake_schema"],
             must_contain_snowflake=[
                 'resource "snowflake_database"',
                 'resource "snowflake_schema"',
                 "ANALYTICS",
                 "PUBLIC",
                 "RAW",
                 "STAGING",
             ],
             must_contain_count={"snowflake_schema": 3},
             notes="Database + 3 schemas; multi-object count check on schemas."),
    # SF03 (Phase 19c) re-enabled. v2 renamed snowflake_role -> snowflake_account_role.
    TestCase("SF03",
             "Create a Snowflake role called DATA_ENGINEER with a descriptive comment.",
             snowflake_types=["snowflake_account_role"],
             must_contain_snowflake=[
                 'resource "snowflake_account_role"',
                 "DATA_ENGINEER",
                 "comment",
             ],
             must_not_contain_snowflake=[
                 'resource "snowflake_role"',
             ],
             notes="v2 rename: snowflake_account_role replaces snowflake_role. Phase 19c re-enabled."),
    TestCase("SF04",
             "Create a Snowflake user called airflow_runner with RSA public key authentication.",
             snowflake_types=["snowflake_user"],
             must_contain_snowflake=[
                 'resource "snowflake_user"',
                 "AIRFLOW_RUNNER",
                 "rsa_public_key",
             ],
             must_not_contain_snowflake=['password ='],
             notes="User with key-pair auth; no password attribute (Snowflake forces key-pair Nov 2025)."),
    TestCase("SF05",
             "Grant the DATA_ENGINEER role to user airflow_runner in Snowflake.",
             snowflake_types=["snowflake_grant_account_role"],
             must_contain_snowflake=[
                 'resource "snowflake_grant_account_role"',
                 "role_name",
                 "DATA_ENGINEER",
             ],
             must_not_contain_snowflake=[
                 'resource "snowflake_role_grants"',
                 'resource "snowflake_role"',
             ],
             notes="Use snowflake_grant_account_role (not deprecated snowflake_role_grants); v2 references must point at snowflake_account_role."),
    TestCase("SF06",
             "Grant USAGE on database Analytics and SELECT on all tables in schema Analytics.PUBLIC to the DATA_ENGINEER role.",
             snowflake_types=["snowflake_grant_privileges_to_account_role"],
             must_contain_snowflake=[
                 'resource "snowflake_grant_privileges_to_account_role"',
                 "USAGE",
                 "SELECT",
             ],
             must_not_contain_snowflake=[
                 'resource "snowflake_account_grant"',
                 'resource "snowflake_schema_grant"',
                 'resource "snowflake_role"',
             ],
             notes="Privilege grant (not deprecated account_grant / schema_grant); v2 references must point at snowflake_account_role."),
    # SF07 (Phase 19c) re-enabled. v2 dropped the `warehouses` attribute on
    # snowflake_resource_monitor; warehouse-to-monitor binding now flows
    # through `resource_monitor` on the warehouse resource.
    TestCase("SF07",
             "Create a single Snowflake resource monitor named BI_BUDGET. Set credit_quota to 100 and suspend_trigger to 100. Set notify_triggers to a list containing 80.",
             snowflake_types=["snowflake_resource_monitor"],
             must_contain_snowflake=[
                 'resource "snowflake_resource_monitor"',
                 "BI_BUDGET",
                 "credit_quota",
                 "100",
                 "notify_triggers",
                 "suspend_trigger",
             ],
             must_not_contain_snowflake=[
                 # v2 schema does not accept `warehouses = [...]` on the monitor.
                 # The cached provider rejects it with "Unsupported argument".
                 "warehouses =",
                 "warehouses  =",
                 "warehouses   =",
                 "warehouses    =",
             ],
             notes="v2 resource_monitor: credit_quota + notify_triggers + suspend_trigger; no warehouses attribute. Phase 19c re-enabled."),
    TestCase("SF08",
             "Create a Snowflake network policy that allows only office IP range 203.0.113.0/24.",
             snowflake_types=["snowflake_network_policy"],
             must_contain_snowflake=[
                 'resource "snowflake_network_policy"',
                 "allowed_ip_list",
                 "203.0.113.0/24",
             ],
             notes="Network policy with allowed_ip_list."),
    # SF09 (Phase 19c) re-enabled. v2 made `enabled` REQUIRED and changed
    # `sync_password` from bool to string.
    TestCase("SF09",
             "Create a Snowflake SCIM integration for Okta provisioning called OKTA_SCIM, running as the OKTA_PROVISIONER role.",
             snowflake_types=["snowflake_scim_integration"],
             must_contain_snowflake=[
                 'resource "snowflake_scim_integration"',
                 "OKTA_SCIM",
                 'scim_client',
                 "OKTA",
                 "run_as_role",
                 "OKTA_PROVISIONER",
                 # v2 requires enabled.
                 "enabled",
             ],
             must_not_contain_snowflake=[
                 # v2 schema: sync_password is STRING, so the bare-bool form is wrong.
                 'sync_password = false',
                 'sync_password = true',
                 'sync_password  = false',
                 'sync_password  = true',
             ],
             notes="v2 scim_integration: enabled required, sync_password is STRING (\"false\"). Phase 19c re-enabled."),
    TestCase("SF10",
             "Create three Snowflake warehouses: REPORTING_WH, ETL_WH, and AD_HOC_WH, all XSMALL with 60s auto-suspend.",
             snowflake_types=["snowflake_warehouse"],
             must_contain_snowflake=[
                 'resource "snowflake_warehouse"',
                 "REPORTING_WH",
                 "ETL_WH",
                 "AD_HOC_WH",
                 "XSMALL",
             ],
             must_contain_count={"snowflake_warehouse": 3},
             notes="Multi-object: 3 distinct warehouse resources."),
    # SF11 (Phase 19c) compound: role + privilege grant. Three resource types
    # in one prompt; exercises the v2 rename + privilege block in one apply.
    TestCase("SF11",
             "Create a Snowflake role called ANALYST and grant it USAGE on database REPORTS and SELECT on all tables in schema REPORTS.PUBLIC.",
             snowflake_types=[
                 "snowflake_account_role",
                 "snowflake_grant_privileges_to_account_role",
             ],
             must_contain_snowflake=[
                 'resource "snowflake_account_role"',
                 "ANALYST",
                 'resource "snowflake_grant_privileges_to_account_role"',
                 "USAGE",
                 "SELECT",
                 "REPORTS",
             ],
             must_contain_count={
                 "snowflake_account_role": 1,
                 "snowflake_grant_privileges_to_account_role": 2,
             },
             must_not_contain_snowflake=[
                 'resource "snowflake_role"',
                 'resource "snowflake_role_grants"',
             ],
             notes="Compound: account_role + 2 privilege grants. Phase 19c."),
    # SF12 (Phase 19c) composite Okta + Snowflake SCIM. Both terraform_okta_hcl
    # and terraform_snowflake_hcl populated.
    TestCase("SF12",
             "Wire SCIM provisioning from Okta into Snowflake: create an okta_app_oauth pointing at the Snowflake SCIM endpoint, and on the Snowflake side create the OKTA_PROVISIONER role and a snowflake_scim_integration called OKTA_SCIM.",
             okta_types=["okta_app_oauth"],
             snowflake_types=[
                 "snowflake_account_role",
                 "snowflake_scim_integration",
             ],
             must_contain=[
                 "okta_app_oauth",
             ],
             must_contain_snowflake=[
                 'resource "snowflake_account_role"',
                 "OKTA_PROVISIONER",
                 'resource "snowflake_scim_integration"',
                 "OKTA_SCIM",
                 "enabled",
                 "run_as_role",
                 # The hand-off note about retrieving the SCIM bearer token must
                 # appear in the snowflake HCL so apply-time operators see it.
                 "SYSTEM$GENERATE_SCIM_ACCESS_TOKEN",
             ],
             must_not_contain_snowflake=[
                 'resource "snowflake_role"',
                 'sync_password = false',
                 'sync_password = true',
                 'sync_password  = false',
                 'sync_password  = true',
             ],
             notes="Composite Okta + Snowflake SCIM: app_oauth + account_role + scim_integration; bearer-token hand-off note required."),

    # ── Kandji (Iru) Terraform via MScottBlake/iru ~> 0.0 (Phase 23) ──────────
    TestCase("KD01",
             "Create a Kandji blueprint called Engineering Mac with a description and the color blue.",
             kandji_types=["iru_blueprint"],
             must_contain_kandji=[
                 'resource "iru_blueprint"',
                 "Engineering Mac",
                 "description",
                 "color",
                 'MScottBlake/iru',
             ],
             must_not_contain_kandji=[
                 'resource "kandji_blueprint"',
                 'kandji-inc/kandji',
             ],
             notes="iru_blueprint top-level container with name + description + color."),
    TestCase("KD02",
             "Create a Kandji custom script library item called Disk Encryption Audit that runs daily and checks FileVault status.",
             kandji_types=["iru_custom_script"],
             must_contain_kandji=[
                 'resource "iru_custom_script"',
                 "execution_frequency",
                 "every_day",
                 "script",
             ],
             must_not_contain_kandji=[
                 'execution_frequency = "daily"',
             ],
             notes="iru_custom_script with execution_frequency canonical value 'every_day' (not 'daily')."),
    TestCase("KD03",
             "Create a Kandji custom profile called WiFi Corporate using an uploaded mobileconfig file that runs on Macs and iPads.",
             kandji_types=["iru_custom_profile"],
             must_contain_kandji=[
                 'resource "iru_custom_profile"',
                 "WiFi Corporate",
                 "profile_file",
                 "runs_on_mac",
                 "runs_on_ipad",
             ],
             notes="iru_custom_profile with profile_file + per-platform runs_on flags."),
    TestCase("KD04",
             "Create a Kandji custom app library item called Slack Desktop that installs once via package, with a postinstall script.",
             kandji_types=["iru_custom_app"],
             must_contain_kandji=[
                 'resource "iru_custom_app"',
                 "Slack Desktop",
                 "file_key",
                 "install_enforcement",
                 "install_once",
                 "install_type",
                 "package",
                 "postinstall_script",
             ],
             notes="iru_custom_app requires all four of file_key + install_enforcement + install_type + name."),
    TestCase("KD05",
             "Create a Kandji tag called executives.",
             kandji_types=["iru_tag"],
             must_contain_kandji=[
                 'resource "iru_tag"',
                 "executives",
             ],
             notes="iru_tag minimal resource: name only."),
    TestCase("KD06",
             "Create a Kandji blueprint called Sales Mac and attach an existing custom script library item with id var.script_item_id to it.",
             kandji_types=["iru_blueprint", "iru_blueprint_library_item"],
             must_contain_kandji=[
                 'resource "iru_blueprint"',
                 'resource "iru_blueprint_library_item"',
                 "blueprint_id",
                 "library_item_id",
             ],
             notes="Blueprint + library-item attachment (the join resource)."),
    TestCase("KD07",
             "Configure Kandji blueprint routing so enrollment codes are active for the whole tenant.",
             kandji_types=["iru_blueprint_routing"],
             must_contain_kandji=[
                 'resource "iru_blueprint_routing"',
                 "enrollment_code_active",
                 "true",
             ],
             must_contain_count={"iru_blueprint_routing": 1},
             notes="iru_blueprint_routing is a tenant-singleton; exactly one block."),
    TestCase("KD08",
             "Create three Kandji blueprints for Engineering, Sales, and Support teams, all with the laptop icon and the same description.",
             kandji_types=["iru_blueprint"],
             must_contain_kandji=[
                 'resource "iru_blueprint"',
                 "Engineering",
                 "Sales",
                 "Support",
                 "icon",
             ],
             must_contain_count={"iru_blueprint": 3},
             notes="Multi-object: 3 distinct iru_blueprint resources."),
    TestCase("KD09",
             "Create a Kandji ADE integration using an Apple Business Manager token file referenced by var.ade_token_file. Set the admin email to admin@example.com and phone to +14155551212. Enable blueprint routing.",
             kandji_types=["iru_ade_integration"],
             must_contain_kandji=[
                 'resource "iru_ade_integration"',
                 "mdm_server_token_file",
                 "email",
                 "admin@example.com",
                 "phone",
                 "use_blueprint_routing",
                 "true",
             ],
             notes="iru_ade_integration: required email + phone + mdm_server_token_file; sensitive token attribute."),
    TestCase("KD10",
             "Set up SAML SSO for Kandji in Okta as an okta_app_saml app, and on the Kandji side create a tag called sso_users and a blueprint called SSO Users Mac.",
             okta_types=["okta_app_saml"],
             kandji_types=["iru_blueprint", "iru_tag"],
             must_contain=[
                 "okta_app_saml",
             ],
             must_contain_kandji=[
                 'resource "iru_blueprint"',
                 'resource "iru_tag"',
                 "SSO Users Mac",
                 "sso_users",
             ],
             notes="Composite Okta + Kandji: SAML app on Okta side + blueprint + tag on Kandji side."),

    # ── Lumos Terraform via teamlumos/lumos ~> 0.10 (Phase 24) ────────────────
    TestCase("LM01",
             "Register a custom Lumos app called Internal Dashboard in the Engineering category with a description explaining it is the team's incident triage dashboard.",
             lumos_types=["lumos_app"],
             must_contain_lumos=[
                 'resource "lumos_app"',
                 "Internal Dashboard",
                 "category",
                 "description",
                 'teamlumos/lumos',
             ],
             must_not_contain_lumos=[
                 'source = "lumos/lumos"',
                 'source = "lumoshq/lumos"',
                 'access_token =',
             ],
             notes="lumos_app top-level custom-app registration with required name + category + description."),

    TestCase("LM02",
             "Install Slack from the Lumos app store using catalog id var.slack_catalog_app_id, with custom request instructions telling users to reach out in #access-help for same-day requests.",
             lumos_types=["lumos_app_store_app"],
             must_contain_lumos=[
                 'resource "lumos_app_store_app"',
                 "app_id",
                 "custom_request_instructions",
                 "access-help",
             ],
             notes="lumos_app_store_app catalog install with custom request instructions."),

    TestCase("LM03",
             "Expose a Lumos requestable permission called Slack workspace admin on the slack_workspace app via var.slack_workspace_app_id.",
             lumos_types=["lumos_requestable_permission"],
             must_contain_lumos=[
                 'resource "lumos_requestable_permission"',
                 "Slack workspace admin",
                 "app_id",
                 "label",
             ],
             notes="lumos_requestable_permission with required app_id + label."),

    TestCase("LM04",
             "Create a Lumos pre-approval rule on the notion_catalog app (id var.notion_catalog_app_id) that auto-approves the engineering group (id var.engineering_lumos_group_id) for one day, seven day, and thirty day access durations. Justification: engineering uses Notion as the canonical wiki and per-request review adds no value.",
             lumos_types=["lumos_pre_approval_rule"],
             must_contain_lumos=[
                 'resource "lumos_pre_approval_rule"',
                 "app_id",
                 "justification",
                 "preapproved_groups",
                 "time_based_access",
                 "1d",
                 "7d",
                 "30d",
             ],
             notes="lumos_pre_approval_rule with preapproved_groups + time_based_access list."),

    TestCase("LM05",
             "Build a Lumos access policy called Support baseline that bundles two Lumos app store apps (zendesk and slack via their catalog ids) and grants them pre-approved at the app level. Business justification: customer support representatives need same-day access to triage incoming tickets.",
             lumos_types=["lumos_access_policy", "lumos_app_store_app"],
             must_contain_lumos=[
                 'resource "lumos_access_policy"',
                 "Support baseline",
                 "business_justification",
                 "apps",
                 "is_preapproved",
             ],
             must_contain_count={"lumos_app_store_app": 2},
             notes="lumos_access_policy bundling two app_store_apps with pre-approved app-level grants."),

    TestCase("LM06",
             "Create a Lumos app store install for Datadog using catalog id var.datadog_catalog_app_id, plus a requestable permission called Datadog admin on it.",
             lumos_types=["lumos_app_store_app", "lumos_requestable_permission"],
             must_contain_lumos=[
                 'resource "lumos_app_store_app"',
                 'resource "lumos_requestable_permission"',
                 "Datadog admin",
             ],
             notes="lumos_app_store_app + lumos_requestable_permission cross-reference; permission.app_id references the app_store_app id."),

    TestCase("LM07",
             "Register three Lumos custom apps for the Sales, Marketing, and Finance internal wikis, all in the Internal Tools category.",
             lumos_types=["lumos_app"],
             must_contain_lumos=[
                 'resource "lumos_app"',
                 "Sales",
                 "Marketing",
                 "Finance",
                 "Internal Tools",
             ],
             must_contain_count={"lumos_app": 3},
             notes="Multi-object: 3 distinct lumos_app resources."),

    TestCase("LM08",
             "Set up a Lumos pre-approval rule on the GitHub Enterprise app (id var.github_catalog_app_id) that pre-approves the engineering group (id var.engineering_lumos_group_id) with one-day and seven-day durations.",
             lumos_types=["lumos_pre_approval_rule"],
             must_contain_lumos=[
                 'resource "lumos_pre_approval_rule"',
                 "preapproved_groups",
                 "time_based_access",
             ],
             must_not_contain_lumos=[
                 'access_token =',
                 'source = "lumos/lumos"',
             ],
             notes="lumos_pre_approval_rule with group + time-based access; provider attr must be http_bearer, not access_token."),

    TestCase("LM09",
             "Create a Lumos custom app called Acme Internal Tool in the Engineering category with description This is the in-house deploy console and the website URL set to var.acme_internal_url.",
             lumos_types=["lumos_app"],
             must_contain_lumos=[
                 'resource "lumos_app"',
                 "Acme Internal Tool",
                 "website_url",
                 "Engineering",
             ],
             notes="lumos_app with optional website_url + required category."),

    TestCase("LM10",
             "Set up SAML SSO for Lumos in Okta as an okta_app_saml app, and on the Lumos side install Slack from the app store (catalog id var.slack_catalog_app_id) and expose a requestable permission called Slack member on it.",
             okta_types=["okta_app_saml"],
             lumos_types=["lumos_app_store_app", "lumos_requestable_permission"],
             must_contain=[
                 "okta_app_saml",
             ],
             must_contain_lumos=[
                 'resource "lumos_app_store_app"',
                 'resource "lumos_requestable_permission"',
                 "Slack member",
             ],
             notes="Composite Okta + Lumos: SAML app on Okta side + app_store install + requestable permission on Lumos side."),
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
    # Strip comment lines so explanatory NOTE/guidance prose mentioning
    # `resource "okta_event_hook"` (e.g. "use okta_event_hook for the
    # remove-from-group case instead") does not trigger event_hook checks
    # on a group_rule output. Mirrors the SCIM block below.
    non_comment_okta_hcl = "\n".join(
        line for line in okta_hcl.split("\n")
        if not line.lstrip().startswith("#")
    )
    if 'resource "okta_event_hook"' in non_comment_okta_hcl:
        for f in FORBIDDEN_EVENT_HOOK_ATTRS:
            if f in non_comment_okta_hcl:
                issues.append(f"Forbidden event hook attribute {f}")
        if "channel" not in non_comment_okta_hcl:
            issues.append("okta_event_hook missing 'channel' block")
        # v4.x schema: `events = [...]` is a flat set attribute (not the
        # legacy `events_filter = { items = [...] }` envelope). Match the
        # attribute via regex anchored at line start so substrings inside
        # other constructs do not satisfy the check.
        if not re.search(r'^\s*events\s*=', non_comment_okta_hcl, re.MULTILINE):
            issues.append("okta_event_hook missing 'events' attribute (v4.x schema)")

    # ── 4. Group membership scenarios must include group.user_membership.* ──
    is_group_scenario = any(kw in tc.prompt.lower() for kw in
                            ["added to", "remove from", "joins the", "mutual exclusiv",
                             "role transition", "only be in one"])
    if is_group_scenario and "okta_event_hook" in non_comment_okta_hcl:
        if "group.user_membership" not in non_comment_okta_hcl:
            issues.append("Group-membership scenario missing group.user_membership.* event — check event types")

    # ── 4a. SCIM provisioning hallucination on app resources ───────────────
    # Strip comment lines so explanatory NOTE blocks (which legitimately mention
    # "provisioning {} block" in prose) don't false-positive.
    if "okta_app_saml" in okta_hcl or "okta_app_oauth" in okta_hcl:
        non_comment_hcl = "\n".join(
            line for line in okta_hcl.split("\n")
            if not line.lstrip().startswith("#")
        )
        for attr in FORBIDDEN_APP_SCIM_ATTRS:
            if attr in non_comment_hcl:
                issues.append(
                    f"Hallucinated SCIM/provisioning attribute '{attr}' on app resource, "
                    f"the v4.x Okta provider has no provisioning block; SCIM is UI-only"
                )

    # ── 4b. Forbidden okta_brand attributes (logo, primary_color, secondary_color) ──
    # The v4.x provider does not support these — apply fails with "Unsupported
    # argument". Scan only inside a `resource "okta_brand"` block so unrelated
    # resources that legitimately use a `logo` attribute aren't false-positived.
    brand_block_match = re.search(
        r'resource\s+"okta_brand"\s+"[^"]+"\s*\{([\s\S]*?)\n\}',
        okta_hcl,
    )
    if brand_block_match:
        body = brand_block_match.group(1)
        body_no_comments = "\n".join(
            line for line in body.split("\n")
            if not line.lstrip().startswith("#")
        )
        for attr in FORBIDDEN_BRAND_ATTRS:
            if re.search(rf'\b{re.escape(attr)}\s*=', body_no_comments) or \
               re.search(rf'\b{re.escape(attr)}\s*\{{', body_no_comments):
                issues.append(
                    f"Forbidden okta_brand attribute '{attr}' — not supported by v4.x provider; "
                    f"logo upload is an Admin Console operation."
                )

    # ── 4b.2 Forbidden okta_network_zone attributes ──────────────────────────
    if 'resource "okta_network_zone"' in okta_hcl:
        for attr in FORBIDDEN_NETWORK_ZONE_ATTRS:
            if re.search(rf'\b{re.escape(attr)}\s*=', okta_hcl):
                issues.append(
                    f"Forbidden okta_network_zone attribute '{attr}' — use `gateways` "
                    f"(IP zones) or `dynamic_locations`/`asns` (DYNAMIC zones) instead."
                )
        # IP/DYNAMIC mutual exclusivity: a single zone declaring both gateways
        # and dynamic_locations/asns is a hallucination of zone shape.
        nz_blocks = re.findall(
            r'resource\s+"okta_network_zone"\s+"[^"]+"\s*\{([\s\S]*?)\n\}',
            okta_hcl,
        )
        for body in nz_blocks:
            has_gateways = re.search(r'\bgateways\s*=', body) or re.search(r'\bgateways\s*\{', body)
            has_dynamic = re.search(r'\bdynamic_locations\s*=', body) or re.search(r'\basns\s*=', body)
            if has_gateways and has_dynamic:
                issues.append(
                    "okta_network_zone mixes `gateways` with `dynamic_locations`/`asns` — "
                    "IP and DYNAMIC zone fields are mutually exclusive."
                )

    # ── 4c. Unescaped Okta Expression Language in HCL string literals ──────
    # `${user.email}` is interpolation in Terraform. Okta Expression Language
    # placeholders must be escaped as `$${user.email}` in source so the literal
    # `${user.email}` ships to Okta. Bare `${...}` fails terraform validate
    # with "Reference to undeclared resource".
    bad_expr_pattern = re.compile(
        r'(subject_name_id_template|user_name_template)\s*=\s*"\$\{[^$][^}]*\}"'
    )
    for m in bad_expr_pattern.finditer(okta_hcl):
        issues.append(
            f"Unescaped Okta Expression Language: `{m.group(0)}`. "
            f"Use `$$` (double dollar) so Terraform does not parse it as an interpolation."
        )

    # ── 4d. SCIM prompt must include the NOTE comment block ───────────────
    # If the prompt mentions SCIM and the output includes okta_app_saml or
    # okta_app_oauth, the output must include a `# NOTE:` comment block that
    # references the Admin Console Provisioning tab (per SECTION F.5 and
    # commit 47a3de6).
    prompt_mentions_scim = "scim" in tc.prompt.lower()
    output_has_app = "okta_app_saml" in okta_hcl or "okta_app_oauth" in okta_hcl
    if prompt_mentions_scim and output_has_app:
        scim_note = re.search(
            r"#\s*NOTE:.*SCIM.*Admin Console.*Provisioning",
            okta_hcl,
            re.IGNORECASE | re.DOTALL,
        )
        if not scim_note:
            issues.append(
                "SCIM prompt missing required `# NOTE:` comment block referencing "
                "Admin Console Provisioning tab (regression of commit 47a3de6)."
            )

    # ── 4f. SCIM SAML prompt must not produce over-scope secondary resources
    # Today's regression: model added okta_group_rule and
    # okta_user_profile_mapping to a "SAML + assign to group" prompt.
    # Per prompts.py:210 allow-list, neither is permitted as a secondary
    # resource for an okta_app_saml intent unless the prompt explicitly
    # asks for them. SCIM substitution via okta_user_profile_mapping is
    # specifically called out as forbidden in SECTION F.5.
    if "okta_app_saml" in okta_hcl and "scim" in tc.prompt.lower():
        prompt_asks_for_rule = bool(re.search(
            r"\b(rule|auto[- ]?assign|matching|for users where)\b",
            tc.prompt,
            re.IGNORECASE,
        ))
        prompt_asks_for_mapping = "profile mapping" in tc.prompt.lower()
        if not prompt_asks_for_rule and "okta_group_rule" in okta_hcl:
            issues.append(
                "Over-scope: okta_group_rule emitted on a SAML+assign prompt "
                "that did not ask for an auto-assignment rule. Group assignment "
                "for a SAML app uses okta_app_group_assignment, never a rule."
            )
        if not prompt_asks_for_mapping and "okta_user_profile_mapping" in okta_hcl:
            issues.append(
                "Over-scope: okta_user_profile_mapping emitted as a SCIM "
                "substitute. SCIM provisioning is UI-only per SECTION F.5 and "
                "the NOTE comment is the only valid response."
            )

    # ── 4e. okta_app_saml must include API-required fields (L2 layer) ─────
    # The Okta backend rejects creates that omit these fields, even though
    # the Terraform provider schema marks them optional. See SECTION G.5.
    # Discovered via apply failure on run 25023847132 (2026-04-27).
    if "okta_app_saml" in okta_hcl:
        saml_blocks = re.findall(
            r'resource\s+"okta_app_saml"\s+"[^"]+"\s*\{[^}]*?\n\}',
            okta_hcl,
            re.DOTALL,
        )
        # Fallback for nested attribute_statements blocks: take everything
        # between the resource opener and the first `^}` at column 0.
        if not saml_blocks:
            saml_blocks = re.findall(
                r'resource\s+"okta_app_saml"\s+"[^"]+"\s*\{.*?\n\}',
                okta_hcl,
                re.DOTALL,
            )
        api_required = [
            "authn_context_class_ref",
            "signature_algorithm",
            "digest_algorithm",
            "honor_force_authn",
        ]
        for block in saml_blocks:
            for field in api_required:
                if field not in block:
                    issues.append(
                        f"okta_app_saml missing API-required field `{field}` "
                        f"(SECTION G.5; apply will fail with 'missing conditionally "
                        f"required fields')."
                    )

    # ── 4g. okta_group_rule expression must use user.X (not user.profile.X) ──
    # Okta's group rule API rejects user.profile.X syntax with "Invalid
    # property profile in expression ..." at apply time (L2 runtime check,
    # not schema). Group rules special-case profile attributes via the
    # shorthand user.X form. Discovered via apply failure on run 25031083752
    # (2026-04-28).
    if "okta_group_rule" in okta_hcl:
        bad_expr_pattern = re.compile(
            r'expression_value\s*=\s*"[^"]*\buser\.profile\.[a-zA-Z_]'
        )
        for m in bad_expr_pattern.finditer(okta_hcl):
            issues.append(
                "okta_group_rule.expression_value uses `user.profile.X` syntax. "
                "Group rules require the shorthand `user.X` form (e.g. "
                "`user.department`, not `user.profile.department`). Apply "
                "fails with `Invalid property profile in expression ...`."
            )

    # ── 4b. okta_group_rule name must be ≤50 chars (provider-enforced) ─────
    if "okta_group_rule" in okta_hcl:
        rule_name_pattern = re.compile(
            r'resource\s+"okta_group_rule"\s+"[^"]+"\s*\{[^}]*?name\s*=\s*"([^"]+)"',
            re.DOTALL,
        )
        for m in rule_name_pattern.finditer(okta_hcl):
            name_val = m.group(1)
            if len(name_val) > 50:
                issues.append(
                    f"okta_group_rule name '{name_val}' exceeds 50 chars "
                    f"(length {len(name_val)}) — Okta provider limit"
                )

    # ── 5. must_contain checks ─────────────────────────────────────────────
    for s in tc.must_contain:
        if s not in okta_hcl:
            issues.append(f"Expected '{s}' not found in terraform_okta_hcl")

    # ── 6. must_not_contain_okta checks ───────────────────────────────────
    for s in tc.must_not_contain_okta:
        if s in okta_hcl:
            issues.append(f"Forbidden string '{s}' found in terraform_okta_hcl")

    # ── 6b. must_contain_count: distinct-resource-block counts across HCLs ─
    # Closes the JF10/COMP02-class drift where the LLM emits one resource
    # block representing several intended instances. Counts `resource "X" "L"`
    # block headers across all four HCL keys.
    if tc.must_contain_count:
        full_hcl = (
            okta_hcl
            + "\n" + (outputs.get("terraform_lambda_hcl", "") or "")
            + "\n" + (outputs.get("terraform_gcp_hcl", "") or "")
            + "\n" + (outputs.get("terraform_jamf_hcl", "") or "")
            + "\n" + (outputs.get("terraform_fleet_hcl", "") or "")
            + "\n" + (outputs.get("terraform_snowflake_hcl", "") or "")
            + "\n" + (outputs.get("terraform_kandji_hcl", "") or "")
            + "\n" + (outputs.get("terraform_lumos_hcl", "") or "")
        )
        for rtype, min_count in tc.must_contain_count.items():
            actual = len(re.findall(rf'resource\s+"{re.escape(rtype)}"\s+"', full_hcl))
            if actual < min_count:
                issues.append(
                    f"Expected at least {min_count} `resource \"{rtype}\"` block(s); found {actual}"
                )

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

    # ── 12. okta_app_saml must not use hallucinated separate attribute resource ──
    if "okta_app_saml" in okta_hcl:
        if re.search(r'resource\s+"okta_app_saml_attribute_statements"', okta_hcl):
            issues.append(
                "Hallucinated resource 'okta_app_saml_attribute_statements' — attribute "
                "statements must be inline blocks inside okta_app_saml, not a separate resource"
            )

    # ── 13. Required-attribute and forbidden-attribute guards (schema reference) ──
    REQUIRED_ATTR_MAP = {
        # redirect_uris not required for service-type apps (client_credentials flow)
        "okta_app_oauth":           ["grant_types"],
        # exact resource match only — avoid substring hits (e.g. okta_auth_server_policy)
        "okta_auth_server":         ["audiences", "issuer_mode"],
        "okta_auth_server_policy":  ["client_whitelist", "priority"],
        "okta_factor":              ["provider_id"],
        "okta_network_zone":        ["type"],
        "okta_email_customization": ["brand_id", "template_name", "body"],
    }
    for resource_type, attrs in REQUIRED_ATTR_MAP.items():
        # Use exact resource declaration match to avoid substring false positives
        if re.search(rf'resource\s+"{resource_type}"', okta_hcl):
            for attr in attrs:
                if not re.search(rf'\b{attr}\b\s*=', okta_hcl):
                    issues.append(f"{resource_type} missing required attribute '{attr}'")

    FORBIDDEN_ATTR_MAP = {
        "okta_app_oauth":           [r"client_id_scheme", r"app_type\s*=", r"client_credentials\s*\{"],
        "okta_auth_server":         [r"\bissuer\s*=", r"\borg_url\s*="],
        "okta_factor":              [r"\bfactor_type\s*=", r"\bpolicy_id\s*=", r"^\s*status\s*=\s*\"ACTIVE\""],
        "okta_network_zone":        [r"\bip_list\s*=", r"\bcidr_ranges\s*="],
        "okta_email_customization": [r"\blocale\s*="],
    }
    for resource_type, patterns in FORBIDDEN_ATTR_MAP.items():
        if re.search(rf'resource\s+"{resource_type}"', okta_hcl):
            for pattern in patterns:
                if re.search(pattern, okta_hcl):
                    issues.append(
                        f"Hallucinated/forbidden attribute (pattern '{pattern}') in {resource_type}"
                    )

    # ── 14. No okta_* resources in terraform_lambda_hcl ─────────────────────
    lambda_hcl = outputs.get("terraform_lambda_hcl", "") or ""
    okta_in_lambda = re.findall(r'resource\s+"(okta_[^"]+)"', lambda_hcl)
    if okta_in_lambda:
        issues.append(f"okta_* resource(s) found in terraform_lambda_hcl: {okta_in_lambda}")

    # ── 15. GCP module checks ───────────────────────────────────────────────
    gcp_hcl = outputs.get("terraform_gcp_hcl", "") or ""

    # 15a. Mode contract: GCP-only mode means everything else empty
    if output_mode == "GCP only":
        if okta_hcl.strip():
            issues.append("terraform_okta_hcl not empty in GCP only mode")
        if lambda_hcl.strip():
            issues.append("terraform_lambda_hcl not empty in GCP only mode")
        if outputs.get("lambda_python", "").strip():
            issues.append("lambda_python not empty in GCP only mode")
    if output_mode == "Okta + GCP":
        if lambda_hcl.strip():
            issues.append("terraform_lambda_hcl not empty in Okta + GCP mode")
        if outputs.get("lambda_python", "").strip():
            issues.append("lambda_python not empty in Okta + GCP mode")

    # 15b. When GCP HCL is non-empty: provider boilerplate + Gen2 + naming + must_contain_gcp
    if gcp_hcl.strip():
        if 'provider "google"' not in gcp_hcl:
            issues.append('terraform_gcp_hcl missing `provider "google"` block')
        # In Okta + GCP composite mode, `merge_terraform_blocks` intentionally
        # moves required_providers entries into terraform_okta_hcl and strips
        # the entire `terraform {}` block from terraform_gcp_hcl. Skip this
        # check there; check the okta side instead.
        if output_mode == "Okta + GCP":
            okta_hcl_check = outputs.get("terraform_okta_hcl", "")
            if 'required_providers' not in okta_hcl_check or 'google = {' not in okta_hcl_check:
                issues.append(
                    "Okta+GCP composite: terraform_okta_hcl must declare both okta and google "
                    "in required_providers after merge_terraform_blocks runs"
                )
        elif 'required_providers' not in gcp_hcl:
            issues.append("terraform_gcp_hcl missing `required_providers` block")

        # Forbidden GCP resources (auth-overwriting IAM policies, Gen1 functions)
        for forbidden in FORBIDDEN_GCP_RESOURCES:
            if re.search(rf'resource\s+"{re.escape(forbidden)}"', gcp_hcl):
                issues.append(
                    f"Forbidden GCP resource '{forbidden}' — see SECTION C2 forbidden list "
                    f"(authoritative IAM policies overwrite project state; Gen1 functions are deprecated)."
                )

        # No okta_* or aws_* in terraform_gcp_hcl
        cross_okta = re.findall(r'resource\s+"(okta_[^"]+)"', gcp_hcl)
        if cross_okta:
            issues.append(f"okta_* resource(s) found in terraform_gcp_hcl: {cross_okta}")
        cross_aws = re.findall(r'resource\s+"(aws_[^"]+)"', gcp_hcl)
        if cross_aws:
            issues.append(f"aws_* resource(s) found in terraform_gcp_hcl: {cross_aws}")

        # 15c. must_contain_gcp / must_not_contain_gcp from the test case
        for needle in tc.must_contain_gcp:
            if needle not in gcp_hcl:
                issues.append(f"Expected '{needle}' in terraform_gcp_hcl")
        for needle in tc.must_not_contain_gcp:
            if needle in gcp_hcl:
                issues.append(f"Forbidden string '{needle}' in terraform_gcp_hcl")

    # 15d. GCP modes must produce non-empty terraform_gcp_hcl
    if output_mode in ("GCP only", "Okta + GCP") and not gcp_hcl.strip():
        issues.append(f"terraform_gcp_hcl empty in {output_mode} mode")

    # ── 16. JAMF Pro checks (terraform_jamf_hcl) ──────────────────────────
    jamf_hcl = outputs.get("terraform_jamf_hcl", "") or ""

    # 16a. Mode boundaries
    if output_mode == "JAMF only":
        if outputs.get("terraform_okta_hcl", "").strip():
            issues.append("terraform_okta_hcl not empty in JAMF only mode")
        if outputs.get("terraform_lambda_hcl", "").strip():
            issues.append("terraform_lambda_hcl not empty in JAMF only mode")
        if outputs.get("terraform_gcp_hcl", "").strip():
            issues.append("terraform_gcp_hcl not empty in JAMF only mode")
    if output_mode == "Okta + JAMF":
        if outputs.get("terraform_lambda_hcl", "").strip():
            issues.append("terraform_lambda_hcl not empty in Okta + JAMF mode")
        if outputs.get("terraform_gcp_hcl", "").strip():
            issues.append("terraform_gcp_hcl not empty in Okta + JAMF mode")

    # 16b. JAMF HCL non-empty: provider, runbook, _v2 smart groups, no yohan460
    if jamf_hcl.strip():
        if 'provider "jamfpro"' not in jamf_hcl:
            issues.append('terraform_jamf_hcl missing `provider "jamfpro"` block')
        # In composite Okta+JAMF mode the merged `required_providers` block
        # is deduped into okta.tf, so the source string can legitimately live
        # there rather than in jamf.tf. Treat both files as the workspace.
        _source_scope = jamf_hcl + "\n" + (outputs.get("terraform_okta_hcl", "") or "")
        if "deploymenttheory/jamfpro" not in _source_scope:
            issues.append("terraform_jamf_hcl (or companion okta.tf in composite mode) missing required source `deploymenttheory/jamfpro`")
        if "yohan460" in jamf_hcl.lower():
            issues.append("terraform_jamf_hcl references rejected provider yohan460/jamf")
        if "JAMF APPLY RUNBOOK" not in jamf_hcl:
            issues.append("terraform_jamf_hcl missing `JAMF APPLY RUNBOOK` comment block")
        if "parallelism=1" not in jamf_hcl:
            issues.append("terraform_jamf_hcl missing required runbook hint `parallelism=1`")
        if "jamfpro_load_balancer_lock = true" not in jamf_hcl:
            issues.append("terraform_jamf_hcl missing required `jamfpro_load_balancer_lock = true` for Cloud safety")
        # Detect v1 smart group resource (legacy)
        if re.search(r'resource\s+"jamfpro_smart_computer_group"\s', jamf_hcl):
            issues.append("terraform_jamf_hcl uses legacy jamfpro_smart_computer_group (v1); use jamfpro_smart_computer_group_v2")
        # No okta_* / aws_* / google_* in jamf hcl
        cross_okta = re.findall(r'resource\s+"(okta_[^"]+)"', jamf_hcl)
        if cross_okta:
            issues.append(f"okta_* resource(s) found in terraform_jamf_hcl: {cross_okta}")
        cross_gcp = re.findall(r'resource\s+"(google_[^"]+)"', jamf_hcl)
        if cross_gcp:
            issues.append(f"google_* resource(s) found in terraform_jamf_hcl: {cross_gcp}")

        # 16c. must_contain_jamf / must_not_contain_jamf
        for needle in tc.must_contain_jamf:
            if needle not in jamf_hcl:
                issues.append(f"Expected '{needle}' in terraform_jamf_hcl")
        for needle in tc.must_not_contain_jamf:
            if needle in jamf_hcl:
                issues.append(f"Forbidden string '{needle}' in terraform_jamf_hcl")

    # 16d. JAMF modes must produce non-empty terraform_jamf_hcl
    if output_mode in ("JAMF only", "Okta + JAMF") and not jamf_hcl.strip():
        issues.append(f"terraform_jamf_hcl empty in {output_mode} mode")

    # ── 17. Fleet GitOps YAML checks (Fleet GitOps only / Okta + Fleet GitOps) ──
    fleet_yaml = outputs.get("fleet_gitops_yaml", "") or ""
    if output_mode in ("Fleet GitOps only", "Okta + Fleet GitOps"):
        if not fleet_yaml.strip():
            issues.append(f"fleet_gitops_yaml empty in {output_mode} mode")
        else:
            # Mandatory apply-runbook header.
            if "# FLEET GITOPS APPLY RUNBOOK" not in fleet_yaml:
                issues.append("fleet_gitops_yaml missing `# FLEET GITOPS APPLY RUNBOOK` header")
            if "fleetctl apply -f default.yml --dry-run" not in fleet_yaml:
                issues.append("fleet_gitops_yaml apply runbook missing `fleetctl apply --dry-run` line")
            # Structural validation via fleet_validate (analog of terraform validate).
            try:
                from fleet_validate import validate_fleet_yaml
                ok, msg = validate_fleet_yaml(fleet_yaml)
                if not ok:
                    issues.append(f"fleet_validate: {msg}")
            except Exception as e:
                issues.append(f"fleet_validate raised: {e}")
        for needle in tc.must_contain_fleet:
            if needle not in fleet_yaml:
                issues.append(f"Expected '{needle}' in fleet_gitops_yaml")
        for needle in tc.must_not_contain_fleet:
            if needle in fleet_yaml:
                issues.append(f"Forbidden string '{needle}' in fleet_gitops_yaml")
        # Mode contract: TF Fleet output must be empty in GitOps modes
        if (outputs.get("terraform_fleet_hcl") or "").strip():
            issues.append(f"terraform_fleet_hcl not empty in {output_mode} mode (expected empty; TF is for `Fleet TF only` modes)")

    # ── 18. Fleet Terraform HCL checks (Fleet TF only / Okta + Fleet TF) ────────
    fleet_hcl = outputs.get("terraform_fleet_hcl", "") or ""
    if output_mode in ("Fleet TF only", "Okta + Fleet TF"):
        if not fleet_hcl.strip():
            issues.append(f"terraform_fleet_hcl empty in {output_mode} mode")
        else:
            # Mandatory experimental warning + apply runbook headers.
            if "EXPERIMENTAL FLEET PROVIDER WARNING" not in fleet_hcl:
                issues.append("terraform_fleet_hcl missing `# EXPERIMENTAL FLEET PROVIDER WARNING` block")
            if "# FLEET TF APPLY RUNBOOK" not in fleet_hcl:
                issues.append("terraform_fleet_hcl missing `# FLEET TF APPLY RUNBOOK` block")
            # In composite mode (`Okta + Fleet TF`) the merged required_providers
            # block lives in okta.tf, so the source string + version pin can
            # legitimately live there rather than in fleet.tf. Treat both files
            # as the workspace (mirrors the JF11 / Snowflake composite pattern).
            _fleet_scope = fleet_hcl + "\n" + (outputs.get("terraform_okta_hcl", "") or "")
            # Exact provider source declaration.
            if 'source  = "l-teles/fleetdm"' not in _fleet_scope and 'source = "l-teles/fleetdm"' not in _fleet_scope:
                issues.append("terraform_fleet_hcl (or companion okta.tf in composite mode) missing `source = \"l-teles/fleetdm\"` provider declaration")
            # Exact version pin: only `version = "0.5.4"` is acceptable, range
            # constraints rejected because the provider is in preview.
            if 'version = "0.5.4"' not in _fleet_scope:
                issues.append("workspace must pin `version = \"0.5.4\"` exactly (provider is preview; range constraints rejected)")
            # Reject any range constraint pointing at the 0.5 series. Match the
            # whole declaration form to avoid colliding with prose mentions of
            # the same character sequence (the prior implementation matched the
            # bare substring and tripped on its own warning comment).
            import re as _re_fleet
            if _re_fleet.search(r'version\s*=\s*"~>\s*0\.5"', _fleet_scope):
                issues.append("workspace uses range version `~> 0.5` for l-teles/fleetdm; provider is preview, must pin to 0.5.4 exact")
            # Deprecated alias checks (cached README: fleetdm_team -> fleetdm_fleet,
            # fleetdm_query -> fleetdm_report).
            if 'resource "fleetdm_team"' in fleet_hcl:
                issues.append("terraform_fleet_hcl uses deprecated `fleetdm_team`; emit `fleetdm_fleet` instead")
            if 'resource "fleetdm_query"' in fleet_hcl:
                issues.append("terraform_fleet_hcl uses deprecated `fleetdm_query`; emit `fleetdm_report` instead")
        for needle in tc.must_contain_fleet_tf:
            if needle not in fleet_hcl:
                issues.append(f"Expected '{needle}' in terraform_fleet_hcl")
        for needle in tc.must_not_contain_fleet_tf:
            if needle in fleet_hcl:
                issues.append(f"Forbidden string '{needle}' in terraform_fleet_hcl")
        # Mode contract: GitOps YAML must be empty in TF modes
        if (outputs.get("fleet_gitops_yaml") or "").strip():
            issues.append(f"fleet_gitops_yaml not empty in {output_mode} mode (expected empty; YAML is for `Fleet GitOps only` modes)")

    # ── 19. Snowflake Terraform HCL checks (Snowflake only / Okta + Snowflake) ──
    snowflake_hcl = outputs.get("terraform_snowflake_hcl", "") or ""
    if output_mode in ("Snowflake only", "Okta + Snowflake"):
        if not snowflake_hcl.strip():
            issues.append(f"terraform_snowflake_hcl empty in {output_mode} mode")
        else:
            # Mandatory apply runbook header.
            if "# SNOWFLAKE APPLY RUNBOOK" not in snowflake_hcl:
                issues.append("terraform_snowflake_hcl missing `# SNOWFLAKE APPLY RUNBOOK` header")
            # In composite mode `Okta + Snowflake` the merged required_providers
            # block lives in okta.tf, so the source string can legitimately live
            # there rather than in snowflake.tf. Treat both files as the workspace
            # (mirrors the JF11 / Fleet TF composite pattern).
            _source_scope = snowflake_hcl + "\n" + (outputs.get("terraform_okta_hcl", "") or "")
            if 'snowflakedb/snowflake' not in _source_scope:
                issues.append("terraform_snowflake_hcl (or companion okta.tf in composite mode) missing required source `snowflakedb/snowflake`")
            # Old source name is deprecated; reject it across the workspace.
            if 'Snowflake-Labs/snowflake' in _source_scope:
                issues.append("workspace uses deprecated `Snowflake-Labs/snowflake` source; use `snowflakedb/snowflake` (provider renamed in 2025)")
            # Version pin: must be ~> 2.0 family.
            if '"~> 2.0"' not in _source_scope and 'version = "2.' not in _source_scope:
                issues.append("workspace must pin `snowflake = { version = \"~> 2.0\" }` (v1.x/v0.x have different resource schemas)")
            # Forbidden: password attribute on snowflake_user (Snowflake forces key-pair as of Nov 2025).
            if 'resource "snowflake_user"' in snowflake_hcl and 'password' in snowflake_hcl.lower():
                # Allow `rsa_public_key` but reject literal `password = ` lines.
                import re as _re
                if _re.search(r'^\s*password\s*=', snowflake_hcl, _re.MULTILINE):
                    issues.append("terraform_snowflake_hcl emits `password` on snowflake_user; Snowflake forces key-pair auth (Nov 2025), use rsa_public_key instead")
            # Deprecated grant resources.
            if 'resource "snowflake_role_grants"' in snowflake_hcl:
                issues.append("terraform_snowflake_hcl uses deprecated `snowflake_role_grants`; use `snowflake_grant_account_role` (v1+ rename)")
            if 'resource "snowflake_account_grant"' in snowflake_hcl or 'resource "snowflake_schema_grant"' in snowflake_hcl:
                issues.append("terraform_snowflake_hcl uses deprecated grant resource; use `snowflake_grant_privileges_to_account_role` (v1+ rename)")
        for needle in tc.must_contain_snowflake:
            if needle not in snowflake_hcl:
                issues.append(f"Expected '{needle}' in terraform_snowflake_hcl")
        for needle in tc.must_not_contain_snowflake:
            if needle in snowflake_hcl:
                issues.append(f"Forbidden string '{needle}' in terraform_snowflake_hcl")

    # ── 19b. Kandji (Iru) Terraform HCL checks (Kandji only / Okta + Kandji) ──
    kandji_hcl = outputs.get("terraform_kandji_hcl", "") or ""
    if output_mode in ("Kandji only", "Okta + Kandji"):
        if not kandji_hcl.strip():
            issues.append(f"terraform_kandji_hcl empty in {output_mode} mode")
        else:
            if "# KANDJI APPLY RUNBOOK" not in kandji_hcl:
                issues.append("terraform_kandji_hcl missing `# KANDJI APPLY RUNBOOK` header")
            # In composite `Okta + Kandji` the merged required_providers block
            # lives in okta.tf, so the source string can legitimately live
            # there rather than kandji.tf. Treat both files as the workspace.
            _kandji_scope = kandji_hcl + "\n" + (outputs.get("terraform_okta_hcl", "") or "")
            if 'MScottBlake/iru' not in _kandji_scope:
                issues.append("terraform_kandji_hcl (or companion okta.tf in composite mode) missing required source `MScottBlake/iru`")
            # Old / wrong source paths must not appear.
            if 'kandji-inc/kandji' in _kandji_scope or 'grossi-co/kandji' in _kandji_scope:
                issues.append("workspace uses non-canonical Kandji provider source; use `MScottBlake/iru` (post-rebrand canonical path)")
            # Kandji-prefix resources are wrong; resource prefix is iru_*.
            import re as _re_kj
            if _re_kj.search(r'^\s*resource\s+"kandji_', kandji_hcl, _re_kj.MULTILINE):
                issues.append("terraform_kandji_hcl emits `kandji_*` resources; provider prefix is `iru_*`")
            # Provider attribute name: api_url (binary), NOT base_url.
            if _re_kj.search(r'provider\s+"iru"\s*\{[^}]*base_url\s*=', kandji_hcl, _re_kj.DOTALL):
                issues.append("terraform_kandji_hcl provider block uses `base_url`; the iru provider expects `api_url`")
        for needle in tc.must_contain_kandji:
            if needle not in kandji_hcl:
                issues.append(f"Expected '{needle}' in terraform_kandji_hcl")
        for needle in tc.must_not_contain_kandji:
            if needle in kandji_hcl:
                issues.append(f"Forbidden string '{needle}' in terraform_kandji_hcl")

    # ── 19c. Lumos Terraform HCL checks (Lumos only / Okta + Lumos) ──
    lumos_hcl = outputs.get("terraform_lumos_hcl", "") or ""
    if output_mode in ("Lumos only", "Okta + Lumos"):
        if not lumos_hcl.strip():
            issues.append(f"terraform_lumos_hcl empty in {output_mode} mode")
        else:
            if "# LUMOS APPLY RUNBOOK" not in lumos_hcl:
                issues.append("terraform_lumos_hcl missing `# LUMOS APPLY RUNBOOK` header")
            # In composite `Okta + Lumos` the merged required_providers block
            # lives in okta.tf, so the source string can legitimately live
            # there rather than lumos.tf. Treat both files as the workspace.
            _lumos_scope = lumos_hcl + "\n" + (outputs.get("terraform_okta_hcl", "") or "")
            if 'teamlumos/lumos' not in _lumos_scope:
                issues.append("terraform_lumos_hcl (or companion okta.tf in composite mode) missing required source `teamlumos/lumos`")
            # Old / wrong source paths must not appear.
            if 'lumos/lumos' in _lumos_scope.replace('teamlumos/lumos', '') or 'lumoshq/lumos' in _lumos_scope:
                issues.append("workspace uses non-canonical Lumos provider source; use `teamlumos/lumos`")
            # Provider attribute: http_bearer (binary), NOT access_token.
            import re as _re_lm
            if _re_lm.search(r'provider\s+"lumos"\s*\{[^}]*access_token\s*=', lumos_hcl, _re_lm.DOTALL):
                issues.append("terraform_lumos_hcl provider block uses `access_token`; the lumos provider expects `http_bearer`")
        for needle in tc.must_contain_lumos:
            if needle not in lumos_hcl:
                issues.append(f"Expected '{needle}' in terraform_lumos_hcl")
        for needle in tc.must_not_contain_lumos:
            if needle in lumos_hcl:
                issues.append(f"Forbidden string '{needle}' in terraform_lumos_hcl")

    # ── 20. Universal post-check: Phase 18b secret-shape scanner ──
    # generator.terraform_gen.generate_all attaches its scanner findings
    # under the private `_secret_scan_findings` key. Any non-empty list
    # fails every test class without per-test opt-in, mirroring the
    # zero-tolerance posture for credential leakage in generated code.
    for f in outputs.get("_secret_scan_findings") or []:
        issues.append(
            f"Secret-shape '{f['category']}' detected in {f['key']} "
            f"line {f['line']}: {f['snippet']}"
        )

    return issues


# ──────────────────────────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────────────────────────

def build_intent(tc: TestCase, client, model: str) -> dict:
    intent = parse_intent(tc.prompt, client, model=model, resource_type_hints=tc.okta_types)
    # Parser is an Okta-infrastructure analyst; on GCP/AWS-only prompts (e.g.
    # "Deploy a Cloud Run service") it can return operation_type="unknown".
    # When the test author has supplied explicit type hints we already know the
    # operation is a create, so override the unknown to keep validate_intent
    # from hard-failing at the parser layer.
    if intent.get("operation_type") == "unknown" and (
        tc.gcp_types or tc.aws_types or tc.okta_types or tc.jamf_types
        or tc.fleet_types or tc.fleet_tf_types or tc.snowflake_types
        or tc.kandji_types or tc.lumos_types
    ):
        intent["operation_type"] = "create"
    if tc.okta_types:
        intent["resource_types"] = tc.okta_types
    if tc.aws_types:
        intent["aws_resource_types"] = tc.aws_types
    if tc.gcp_types:
        intent["gcp_resource_types"] = tc.gcp_types
    if tc.jamf_types:
        intent["jamf_resource_types"] = tc.jamf_types
    if tc.fleet_types:
        intent["fleet_resource_types"] = tc.fleet_types
    if tc.fleet_tf_types:
        intent["fleet_resource_types"] = tc.fleet_tf_types  # same parser routing; mode decides format
    if tc.snowflake_types:
        intent["snowflake_resource_types"] = tc.snowflake_types
    if tc.kandji_types:
        intent["kandji_resource_types"] = tc.kandji_types
    if tc.lumos_types:
        intent["lumos_resource_types"] = tc.lumos_types
    # Mode mapping mirrors app.py:Stage 1 (after-parse):
    # lumos+okta -> "Okta + Lumos", lumos alone -> "Lumos only",
    # kandji+okta -> "Okta + Kandji", kandji alone -> "Kandji only",
    # snowflake+okta -> "Okta + Snowflake", snowflake alone -> "Snowflake only",
    # fleet_tf+okta -> "Okta + Fleet TF", fleet_tf alone -> "Fleet TF only",
    # fleet+okta -> "Okta + Fleet GitOps", fleet alone -> "Fleet GitOps only",
    # jamf+okta -> "Okta + JAMF", jamf alone -> "JAMF only",
    # gcp+okta -> "Okta + GCP", gcp alone -> "GCP only", aws+okta -> "Both",
    # okta alone -> "Okta Terraform only", aws alone (rare) -> "Lambda only".
    if tc.lumos_types and tc.okta_types:
        intent["output_mode"] = "Okta + Lumos"
    elif tc.lumos_types:
        intent["output_mode"] = "Lumos only"
    elif tc.kandji_types and tc.okta_types:
        intent["output_mode"] = "Okta + Kandji"
    elif tc.kandji_types:
        intent["output_mode"] = "Kandji only"
    elif tc.snowflake_types and tc.okta_types:
        intent["output_mode"] = "Okta + Snowflake"
    elif tc.snowflake_types:
        intent["output_mode"] = "Snowflake only"
    elif tc.fleet_tf_types and tc.okta_types:
        intent["output_mode"] = "Okta + Fleet TF"
    elif tc.fleet_tf_types:
        intent["output_mode"] = "Fleet TF only"
    elif tc.fleet_types and tc.okta_types:
        intent["output_mode"] = "Okta + Fleet GitOps"
    elif tc.fleet_types:
        intent["output_mode"] = "Fleet GitOps only"
    elif tc.jamf_types and tc.okta_types:
        intent["output_mode"] = "Okta + JAMF"
    elif tc.jamf_types:
        intent["output_mode"] = "JAMF only"
    elif tc.gcp_types and tc.okta_types:
        intent["output_mode"] = "Okta + GCP"
    elif tc.gcp_types:
        intent["output_mode"] = "GCP only"
    elif tc.aws_types and tc.okta_types:
        intent["output_mode"] = "Both"
    elif tc.aws_types:
        intent["output_mode"] = "Lambda only"
    else:
        intent["output_mode"] = "Okta Terraform only"
    intent["answers"] = {}
    intent["provider_version"] = "~> 4.0"
    return intent


def run_test(tc: TestCase, client, model: str, replay_mode: bool = False, passes: int = 1) -> dict:
    start = time.time()
    try:
        if replay_mode:
            if not CACHE_PATH.exists():
                return {
                    "id": tc.id, "status": "ERROR",
                    "issues": ["No cache — run without --replay first"],
                    "elapsed": round(time.time() - start, 1),
                    "attempt_count": 0,
                }
            with open(CACHE_PATH) as f:
                cache = json.load(f)
            if tc.id not in cache:
                return {
                    "id": tc.id, "status": "ERROR",
                    "issues": [f"No cached output for {tc.id}"],
                    "elapsed": round(time.time() - start, 1),
                    "attempt_count": 0,
                }
            entry = cache[tc.id]
            outputs = entry["outputs"]
            intent = entry["intent"]
            issues = run_checks(tc, intent, outputs)
            return {
                "id": tc.id,
                "prompt": tc.prompt,
                "status": "PASS" if not issues else "FAIL",
                "issues": issues,
                "resource_type": intent.get("resource_type"),
                "output_mode": intent.get("output_mode"),
                "elapsed": round(time.time() - start, 1),
                "attempt_count": 1,
            }

        intent = build_intent(tc, client, model)
        val_errors = validate_intent(intent)
        if val_errors:
            return {
                "id": tc.id, "status": "FAIL",
                "issues": [f"Intent validation: {e}" for e in val_errors],
                "resource_type": intent.get("resource_type"),
                "output_mode": intent.get("output_mode"),
                "elapsed": round(time.time() - start, 1),
                "attempt_count": 0,
            }

        best_issues = None
        best_outputs = None
        winning_attempt = passes  # pessimistic default — updated on first pass or on success

        for attempt in range(1, passes + 1):
            outputs = generate_all(intent, extra_instructions="", client=client, model=model)
            issues = run_checks(tc, intent, outputs)
            if best_issues is None or len(issues) < len(best_issues):
                best_issues = issues
                best_outputs = outputs
                winning_attempt = attempt
            if not issues:
                winning_attempt = attempt
                break

        _OUTPUT_CACHE[tc.id] = {
            "outputs": best_outputs,
            "intent": intent,
            "parsed_as": intent.get("resource_type", ""),
        }
        return {
            "id": tc.id,
            "prompt": tc.prompt,
            "status": "PASS" if not best_issues else "FAIL",
            "issues": best_issues or [],
            "resource_type": intent.get("resource_type"),
            "output_mode": intent.get("output_mode"),
            "elapsed": round(time.time() - start, 1),
            "attempt_count": winning_attempt,
        }
    except GenerationError as e:
        return {
            "id": tc.id, "status": "ERROR",
            "issues": [f"GenerationError: {e}"],
            "elapsed": round(time.time() - start, 1),
            "attempt_count": 0,
        }
    except Exception as e:
        return {
            "id": tc.id, "status": "ERROR",
            "issues": [f"{type(e).__name__}: {e}"],
            "elapsed": round(time.time() - start, 1),
            "attempt_count": 0,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Phase 22b: Anthropic Message Batches API integration
# ──────────────────────────────────────────────────────────────────────────────
#
# Batch mode is opt-in via `--batch` and submits one request per test to
# the Anthropic Message Batches API. Each batch request carries the
# parse_intent prompt (system + user message) so the LLM returns the
# structured intent JSON. After the batch ends, the runner:
#
#   1. Decodes each result into an intent dict (matching the same shape
#      that `parse_intent` returns in serial mode).
#   2. Runs the existing post-generation pipeline serially per test;
#      build_intent overrides, generate_all, sanitizers, run_checks,
#      and (when validate_mode is on) terraform validate.
#
# The batch generation step is what gets the 50% discount; the
# subsequent generate_all calls remain serial and are billed at standard
# rates. Operators wanting to batch the generate_all step itself need a
# follow-up phase that intercepts generator internals; this phase
# delivers the API plumbing end-to-end so we can measure the parse
# savings on a real regression and decide whether to invest further.
#
# Polling cadence: 30s for the first 5 minutes, 2 min after that. Hard
# timeout at 24h. Custom IDs are test_ids so partial batch failures map
# back to TestCases cleanly.

_BATCH_USAGE_TOTALS = {
    "calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}

_BATCH_POLL_INITIAL_S = 30
_BATCH_POLL_FAST_PHASE_S = 300  # 5 min
_BATCH_POLL_SLOW_S = 120
_BATCH_TIMEOUT_S = 24 * 60 * 60


def _build_batch_request_for_tc(tc: "TestCase", model: str) -> dict:
    """Translate one TestCase into a `requests=[...]` item for
    `client.messages.batches.create`. Mirrors parser.parse_intent's
    message shape exactly so the batched response can be parsed by
    parser._extract_json without modification.
    """
    from generator.prompts import INTENT_PARSER_SYSTEM_PROMPT, INTENT_USER_PROMPT_TEMPLATE

    hint_section = ""
    hints = tc.okta_types or []
    if hints:
        hint_section = (
            f"\n\nResource types explicitly selected by the user: "
            f"{', '.join(hints)}. Use these to inform resource_type "
            f"selection; prefer one of these types over guessing."
        )
    return {
        "custom_id": tc.id,
        "params": {
            "model": model,
            "max_tokens": 4096,
            "system": [
                {
                    "type": "text",
                    "text": INTENT_PARSER_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": INTENT_USER_PROMPT_TEMPLATE.format(user_input=tc.prompt) + hint_section,
                }
            ],
        },
    }


def _accumulate_batch_usage(usage_obj) -> None:
    """Roll one batch result's usage into the batch-specific totals so
    `--batch` runs can be reported separately from any serial spend."""
    if usage_obj is None:
        return
    _BATCH_USAGE_TOTALS["calls"] += 1
    _BATCH_USAGE_TOTALS["input_tokens"] += getattr(usage_obj, "input_tokens", 0) or 0
    _BATCH_USAGE_TOTALS["output_tokens"] += getattr(usage_obj, "output_tokens", 0) or 0
    _BATCH_USAGE_TOTALS["cache_creation_input_tokens"] += getattr(usage_obj, "cache_creation_input_tokens", 0) or 0
    _BATCH_USAGE_TOTALS["cache_read_input_tokens"] += getattr(usage_obj, "cache_read_input_tokens", 0) or 0


def _print_batch_usage_totals() -> None:
    t = _BATCH_USAGE_TOTALS
    if t["calls"] == 0:
        return
    # Batch responses are billed at 50% of standard input/output rates.
    raw_cost = (
        t["input_tokens"] * HAIKU_4_5_RATES_PER_M["input"]
        + t["output_tokens"] * HAIKU_4_5_RATES_PER_M["output"]
        + t["cache_creation_input_tokens"] * HAIKU_4_5_RATES_PER_M["cache_write"]
        + t["cache_read_input_tokens"] * HAIKU_4_5_RATES_PER_M["cache_read"]
    ) / 1_000_000
    discounted = raw_cost * 0.5
    print()
    print("  [batch-discounted usage]")
    print(f"  API calls            : {t['calls']:,}")
    print(f"  Input (uncached)     : {t['input_tokens']:>10,} tokens")
    print(f"  Output               : {t['output_tokens']:>10,} tokens")
    print(f"  Cache writes         : {t['cache_creation_input_tokens']:>10,} tokens")
    print(f"  Cache reads          : {t['cache_read_input_tokens']:>10,} tokens")
    print(f"  Estimated batch cost : ${discounted:.3f}  (50% off serial: serial-equivalent ${raw_cost:.3f})")


def _decode_batch_result_to_intent(result_entry: dict, tc: "TestCase") -> dict:
    """Apply the same intent_type extraction parse_intent does, then
    apply build_intent's downstream overrides (output_mode, hints,
    resource_types). Returns a fully-stamped intent dict ready for
    generate_all.

    `result_entry` is the dict yielded by `client.messages.batches.results(...)`:
      {"custom_id": "...", "result": {"type": "succeeded", "message": <Message>}}
    """
    from generator.parser import _extract_json, validate_intent  # type: ignore[attr-defined]

    result = result_entry.get("result") or {}
    rtype = result.get("type")
    if rtype != "succeeded":
        # Errored / expired / canceled batch entries surface here.
        raise RuntimeError(f"batch result type={rtype!r} for custom_id={result_entry.get('custom_id')!r}")

    message = result.get("message")
    if message is None:
        raise RuntimeError(f"batch result missing message for custom_id={result_entry.get('custom_id')!r}")

    _accumulate_batch_usage(getattr(message, "usage", None))

    content = getattr(message, "content", None) or []
    if not content:
        raise RuntimeError("batch result message has empty content")
    raw_text = getattr(content[0], "text", "") or ""
    raw = _extract_json(raw_text)
    try:
        intent = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"batch result did not return JSON: {raw[:300]}") from e

    # Apply build_intent's overrides so the resulting intent is the same
    # shape generate_all expects.
    if intent.get("operation_type") == "unknown" and (
        tc.gcp_types or tc.aws_types or tc.okta_types or tc.jamf_types
        or tc.fleet_types or tc.fleet_tf_types or tc.snowflake_types
        or tc.kandji_types or tc.lumos_types
    ):
        intent["operation_type"] = "create"
    if tc.okta_types:
        intent["resource_types"] = tc.okta_types
    if tc.aws_types:
        intent["aws_resource_types"] = tc.aws_types
    if tc.gcp_types:
        intent["gcp_resource_types"] = tc.gcp_types
    if tc.jamf_types:
        intent["jamf_resource_types"] = tc.jamf_types
    if tc.fleet_types:
        intent["fleet_resource_types"] = tc.fleet_types
    if tc.fleet_tf_types:
        intent["fleet_resource_types"] = tc.fleet_tf_types
    if tc.snowflake_types:
        intent["snowflake_resource_types"] = tc.snowflake_types
    if tc.snowflake_types and tc.okta_types:
        intent["output_mode"] = "Okta + Snowflake"
    elif tc.snowflake_types:
        intent["output_mode"] = "Snowflake only"
    elif tc.fleet_tf_types and tc.okta_types:
        intent["output_mode"] = "Okta + Fleet TF"
    elif tc.fleet_tf_types:
        intent["output_mode"] = "Fleet TF only"
    elif tc.fleet_types and tc.okta_types:
        intent["output_mode"] = "Okta + Fleet GitOps"
    elif tc.fleet_types:
        intent["output_mode"] = "Fleet GitOps only"
    elif tc.jamf_types and tc.okta_types:
        intent["output_mode"] = "Okta + JAMF"
    elif tc.jamf_types:
        intent["output_mode"] = "JAMF only"
    elif tc.gcp_types and tc.okta_types:
        intent["output_mode"] = "Okta + GCP"
    elif tc.gcp_types:
        intent["output_mode"] = "GCP only"
    elif tc.aws_types and tc.okta_types:
        intent["output_mode"] = "Both"
    elif tc.aws_types:
        intent["output_mode"] = "Lambda only"
    else:
        intent["output_mode"] = "Okta Terraform only"
    intent["answers"] = {}
    intent["provider_version"] = "~> 4.0"
    return intent


def _poll_batch_until_done(client, batch_id: str, *, sleep_fn=time.sleep, now_fn=time.time) -> str:
    """Poll batches.retrieve until processing_status leaves "in_progress"
    / "canceling". Returns the final processing_status. Raises
    TimeoutError on 24h timeout."""
    start = now_fn()
    elapsed = 0
    next_poll = _BATCH_POLL_INITIAL_S
    while True:
        if elapsed >= _BATCH_TIMEOUT_S:
            raise TimeoutError(f"batch {batch_id} did not finish within 24h")
        b = client.messages.batches.retrieve(batch_id)
        status = getattr(b, "processing_status", None) or "in_progress"
        if status not in ("in_progress", "canceling"):
            return status
        sleep_fn(next_poll)
        elapsed = now_fn() - start
        next_poll = _BATCH_POLL_INITIAL_S if elapsed < _BATCH_POLL_FAST_PHASE_S else _BATCH_POLL_SLOW_S


def run_batch(cases: list, client, model: str) -> dict[str, dict]:
    """Submit a single batch with one request per test, poll, and return
    a mapping of test_id -> intent dict (or an error entry).

    Error shape: `{"_error": "..."}` so the per-test loop downstream can
    surface partial-batch failures without aborting the whole run. A
    test that returned non-JSON or hit an errored result yields one of
    these stub dicts; the caller turns it into a FAIL/ERROR row.
    """
    if not cases:
        return {}
    requests = [_build_batch_request_for_tc(tc, model) for tc in cases]
    print(f"  Submitting batch with {len(requests)} request(s) ...")
    submitted = client.messages.batches.create(requests=requests)
    batch_id = getattr(submitted, "id", None)
    if not batch_id:
        raise RuntimeError("batches.create returned no id")
    print(f"  Batch id: {batch_id}")
    final_status = _poll_batch_until_done(client, batch_id)
    print(f"  Batch finished: processing_status={final_status}")

    out: dict[str, dict] = {}
    succeeded = errored = 0
    for entry in client.messages.batches.results(batch_id):
        # `entry` may be an SDK object with attribute access; coerce to
        # dict for uniform handling.
        if hasattr(entry, "model_dump"):
            entry = entry.model_dump()
        elif not isinstance(entry, dict):
            entry = {
                "custom_id": getattr(entry, "custom_id", None),
                "result": getattr(entry, "result", None),
            }
        custom_id = entry.get("custom_id") or ""
        tc = next((c for c in cases if c.id == custom_id), None)
        if tc is None:
            continue
        try:
            intent = _decode_batch_result_to_intent(entry, tc)
            out[custom_id] = intent
            succeeded += 1
        except Exception as e:
            out[custom_id] = {"_error": f"{type(e).__name__}: {e}"}
            errored += 1
    print(f"  Decoded: {succeeded} succeeded, {errored} errored")
    return out


def run_test_with_batched_intent(tc: "TestCase", intent_or_error: dict, client, model: str) -> dict:
    """Mirror of run_test but the intent was produced by the batch step.
    Skips parse_intent (already done in batch), runs the generate+refine
    pipeline serially, and returns the same result-row shape run_test
    emits."""
    start = time.time()
    if "_error" in intent_or_error:
        return {
            "id": tc.id,
            "status": "ERROR",
            "issues": [f"BatchError: {intent_or_error['_error']}"],
            "elapsed": round(time.time() - start, 1),
            "attempt_count": 0,
        }
    try:
        intent = intent_or_error
        from generator.parser import validate_intent
        val_errors = validate_intent(intent)
        if val_errors:
            return {
                "id": tc.id, "status": "FAIL",
                "issues": [f"Intent validation: {e}" for e in val_errors],
                "resource_type": intent.get("resource_type"),
                "output_mode": intent.get("output_mode"),
                "elapsed": round(time.time() - start, 1),
                "attempt_count": 0,
            }
        outputs = generate_all(intent, extra_instructions="", client=client, model=model)
        issues = run_checks(tc, intent, outputs)
        _OUTPUT_CACHE[tc.id] = {
            "outputs": outputs,
            "intent": intent,
            "parsed_as": intent.get("resource_type", ""),
        }
        return {
            "id": tc.id,
            "prompt": tc.prompt,
            "status": "PASS" if not issues else "FAIL",
            "issues": issues,
            "resource_type": intent.get("resource_type"),
            "output_mode": intent.get("output_mode"),
            "elapsed": round(time.time() - start, 1),
            "attempt_count": 1,
        }
    except GenerationError as e:
        return {
            "id": tc.id, "status": "ERROR",
            "issues": [f"GenerationError: {e}"],
            "elapsed": round(time.time() - start, 1),
            "attempt_count": 0,
        }
    except Exception as e:
        return {
            "id": tc.id, "status": "ERROR",
            "issues": [f"{type(e).__name__}: {e}"],
            "elapsed": round(time.time() - start, 1),
            "attempt_count": 0,
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


def _parse_passes(argv: list[str]) -> tuple[int, set[int]]:
    """Return (passes_value, indices_of_flag_values_to_skip_in_filter_parsing)."""
    for i, a in enumerate(argv):
        if a == "--passes" and i + 1 < len(argv):
            try:
                return int(argv[i + 1]), {i + 1}
            except ValueError:
                pass
    return 1, set()


def _capture_validate_diagnostics(workdir, env) -> list[dict]:
    """Re-run `terraform validate -json` on a failed workspace to capture the
    full diagnostic objects (file, line, summary, detail). Used by
    `_run_terraform_validate` to enrich the failing-tests summary so the
    log shows the actionable attribute name ("An argument named
    'token_lifetime' is not expected here.") instead of just the truncated
    "Unsupported argument" first-line excerpt.

    Returns a list of diagnostic dicts. Each dict has keys: `severity`,
    `summary`, `detail`, `range` (a dict with `filename`, `start`, `end`).
    Returns [] when the JSON parse fails or terraform validate is
    unavailable.
    """
    import subprocess
    import json as _json
    try:
        val = subprocess.run(
            ["terraform", "validate", "-json", "-no-color"],
            cwd=workdir, env=env, capture_output=True, text=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    raw = val.stdout or ""
    try:
        parsed = _json.loads(raw)
    except _json.JSONDecodeError:
        return []
    diags = parsed.get("diagnostics") or []
    if not isinstance(diags, list):
        return []
    # Keep only `error` severity; warnings are noise for the failure
    # summary. Limit to the first 5 to bound report size on profiles with
    # many missing required attributes (e.g. JAMF plist generator).
    errors = [d for d in diags if isinstance(d, dict) and d.get("severity") == "error"]
    return errors[:5]


def _run_terraform_validate(results: list[dict], outputs_by_id: dict) -> tuple[int, int, int]:
    """Run terraform init + validate against each test's generated HCL.
    Mutates `results` in place: adds `terraform_validate_pass` (bool|None),
    `terraform_validate_error` (str|None), and `terraform_validate_diagnostics`
    (list of diagnostic dicts on failure, [] on pass, None on skip). On a
    validate failure also appends a "terraform validate: ..." entry to
    `issues` and flips status to FAIL. Returns (passed, failed, skipped)
    counts."""
    from tf_validate import (
        PLUGIN_CACHE,
        WORKSPACE_ROOT,
        make_env,
        run_terraform,
        write_workspace,
    )

    env = make_env()
    print(f"\nterraform-validate: {len(results)} test(s)")
    print(f"  plugin cache: {PLUGIN_CACHE}")
    print(f"  workspaces:   {WORKSPACE_ROOT}")
    print("=" * 72)

    passed = failed = skipped = 0
    for r in results:
        tid = r["id"]
        outputs = outputs_by_id.get(tid)
        has_hcl = outputs and any(
            (outputs.get(k) or "").strip()
            for k in ("terraform_okta_hcl", "terraform_lambda_hcl", "terraform_gcp_hcl", "terraform_jamf_hcl", "terraform_fleet_hcl", "terraform_snowflake_hcl", "terraform_kandji_hcl", "terraform_lumos_hcl")
        )
        if not has_hcl:
            r["terraform_validate_pass"] = None
            r["terraform_validate_error"] = None
            r["terraform_validate_diagnostics"] = None
            skipped += 1
            print(f"  {tid:<8} SKIP (no HCL)")
            continue
        workdir = write_workspace(tid, outputs)
        ok, msg = run_terraform(workdir, env)
        r["terraform_validate_pass"] = ok
        r["terraform_validate_error"] = None if ok else msg
        if ok:
            passed += 1
            r["terraform_validate_diagnostics"] = []
            print(f"  {tid:<8} PASS")
        else:
            failed += 1
            # Phase 20: re-run with `-json` to capture full diagnostic
            # objects so the failing-tests summary can show the actionable
            # attribute name ("token_lifetime") instead of just the
            # generic "Unsupported argument" header.
            diags = _capture_validate_diagnostics(workdir, env)
            r["terraform_validate_diagnostics"] = diags
            print(f"  {tid:<8} FAIL  {msg}")
            r.setdefault("issues", []).append(f"terraform validate: {msg}")
            # Append the first 3 diagnostic detail strings to issues so the
            # final failing-tests summary surfaces the actionable attr name.
            for d in diags[:3]:
                detail = (d.get("detail") or "").strip()
                summary = (d.get("summary") or "").strip()
                rng = d.get("range") or {}
                start = rng.get("start") or {}
                where = ""
                if rng.get("filename"):
                    where = f" [{rng['filename']}:{start.get('line', '?')}]"
                if detail:
                    r["issues"].append(f"  diag: {summary}: {detail}{where}")
                elif summary:
                    r["issues"].append(f"  diag: {summary}{where}")
            if r.get("status") == "PASS":
                r["status"] = "FAIL"
    print("=" * 72)
    return passed, failed, skipped


def main():
    argv = sys.argv[1:]
    replay_mode = "--replay" in argv
    batch_mode = "--batch" in argv
    # As of 2026-05-04 Both score is 123/133 with the validate guardrail
    # ON; running it by default makes every QA invocation catch real
    # provider-schema drift, not just must_contain string mismatches.
    # `--no-terraform-validate` opts out for the rare case where you want
    # the faster string-only path (e.g. iterating on a single prompt
    # before a full validate sweep).
    validate_mode = "--no-terraform-validate" not in argv
    passes, skip_indices = _parse_passes(argv)
    filter_ids = set(
        a.upper() for i, a in enumerate(argv)
        if not a.startswith("--") and i not in skip_indices
    )
    cases = [tc for tc in TEST_CASES if not filter_ids or tc.id.upper() in filter_ids]

    if batch_mode and replay_mode:
        print("ERROR: --batch and --replay are mutually exclusive")
        sys.exit(2)
    if batch_mode and passes > 1:
        print("ERROR: --batch does not support multi-pass mode (--passes > 1)")
        sys.exit(2)

    validate_label = " + terraform-validate" if validate_mode else " (validate disabled)"
    mode_label = ""
    if batch_mode:
        mode_label = " (batch)"
    if replay_mode:
        client = None
        model = None
        print(f"QA runner — REPLAY MODE — {len(cases)} tests{validate_label} — reading from qa_outputs_cache.json")
    else:
        api_key = _read_api_key()
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY not found. Set it in the environment or .streamlit/secrets.toml")
            sys.exit(1)
        client = _wrap_client_for_usage_tracking(anthropic.Anthropic(api_key=api_key))
        model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
        passes_label = f" — passes: {passes}" if passes > 1 else ""
        print(f"QA runner — {len(cases)} tests — model: {model}{passes_label}{validate_label}{mode_label}")
    print("=" * 72)

    results = []
    passed = failed = errored = 0

    # In batch mode, submit + poll + decode the parse_intent step for
    # every test in one batches.create call, then run the generate +
    # post-generation pipeline serially.
    batched_intents: dict[str, dict] = {}
    if batch_mode and not replay_mode:
        batched_intents = run_batch(cases, client, model)
        print("=" * 72)

    for i, tc in enumerate(cases, 1):
        label = f"[{i:02d}/{len(cases)}] {tc.id:<6} {tc.prompt[:55]:<55}"
        print(f"{label} ...", end="", flush=True)
        if batch_mode and not replay_mode:
            intent_or_err = batched_intents.get(tc.id, {"_error": "no batch result for this test_id"})
            r = run_test_with_batched_intent(tc, intent_or_err, client, model)
        else:
            r = run_test(tc, client, model, replay_mode=replay_mode, passes=passes)
        results.append(r)
        elapsed = r.get("elapsed", 0)
        attempt_tag = f" [#{r.get('attempt_count', 1)}]" if passes > 1 and r["status"] == "PASS" else ""
        if r["status"] == "PASS":
            passed += 1
            print(f"\r{label} PASS{attempt_tag}  ({elapsed}s)")
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

    _print_usage_totals()
    _print_batch_usage_totals()

    v_passed = v_failed = v_skipped = 0
    if validate_mode:
        # Snapshot static-QA pass count BEFORE validate, since validate may
        # flip PASS -> FAIL when re-tallying for the exit code below.
        static_passed = passed
        outputs_by_id: dict[str, dict] = {}
        if replay_mode:
            if CACHE_PATH.exists():
                with open(CACHE_PATH) as f:
                    cache = json.load(f)
                outputs_by_id = {tid: entry["outputs"] for tid, entry in cache.items() if "outputs" in entry}
        else:
            outputs_by_id = {tid: entry["outputs"] for tid, entry in _OUTPUT_CACHE.items() if "outputs" in entry}
        v_passed, v_failed, v_skipped = _run_terraform_validate(results, outputs_by_id)
        passed = sum(1 for r in results if r["status"] == "PASS")
        failed = sum(1 for r in results if r["status"] == "FAIL")
        errored = sum(1 for r in results if r["status"] not in ("PASS", "FAIL"))
        v_total = v_passed + v_failed
        skip_note = f" (skipped {v_skipped})" if v_skipped else ""
        print(f"\n  Static QA           : {static_passed}/{len(cases)}")
        print(f"  Terraform validate  : {v_passed}/{v_total}{skip_note}")
        print(f"  Both                : {passed}/{len(cases)}")

    if passes > 1:
        pass_at_1 = sum(1 for r in results if r["status"] == "PASS" and r.get("attempt_count", 1) == 1)
        pass_at_n = passed
        never_passed = failed + errored
        winning_counts = [r["attempt_count"] for r in results if r["status"] == "PASS" and r.get("attempt_count", 0) > 0]
        med = statistics.median(winning_counts) if winning_counts else 0
        print(f"\n  pass@1  : {pass_at_1}/{len(cases)} ({100*pass_at_1/len(cases):.1f}%)")
        print(f"  pass@{passes} : {pass_at_n}/{len(cases)} ({100*pass_at_n/len(cases):.1f}%)")
        print(f"  median attempts to first pass: {med}")
        print(f"  never passed: {never_passed}")

    print("=" * 72)

    if failed or errored:
        print("\nFailing tests summary:")
        for r in results:
            if r["status"] != "PASS":
                rt = r.get("resource_type", "?")
                print(f"  {r['id']:<6} [{r['status']}]  parsed_as={rt}")
                for iss in r.get("issues", []):
                    print(f"         -> {iss}")

    if not replay_mode and _OUTPUT_CACHE:
        with open(CACHE_PATH, "w") as f:
            json.dump(_OUTPUT_CACHE, f, indent=2)

    report_path = os.path.join(os.path.dirname(__file__), "qa_report.json")
    with open(report_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nReport saved: {report_path}")

    sys.exit(0 if (failed + errored) == 0 else 1)


if __name__ == "__main__":
    main()
