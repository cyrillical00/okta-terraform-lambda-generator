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
    expected_resource_type: Optional[str] = None
    # strings that MUST appear in terraform_okta_hcl
    must_contain: list = field(default_factory=list)
    # strings that must NOT appear in terraform_okta_hcl
    must_not_contain_okta: list = field(default_factory=list)
    # strings that MUST appear in terraform_gcp_hcl (GCP/Okta+GCP modes)
    must_contain_gcp: list = field(default_factory=list)
    # strings that must NOT appear in terraform_gcp_hcl
    must_not_contain_gcp: list = field(default_factory=list)
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

FORBIDDEN_EVENT_HOOK_ATTRS = ['"events"', '"filters"', '"auth_type"']

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
             must_contain=["okta_app_saml", "okta_group"]),
    TestCase("COMP03",
             "Create an auth server for our payments API with a payments:write scope and a role claim",
             expected_resource_type="okta_auth_server",
             must_contain=["okta_auth_server", "okta_auth_server_scope", "okta_auth_server_claim"]),

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

    # ── okta_user_profile_mapping ─────────────────────────────────────────────
    TestCase("PM01", "Map the department attribute from the Workday app to the Okta user profile",
             expected_resource_type="okta_user_profile_mapping",
             must_contain=["okta_user_profile_mapping"]),
    TestCase("PM02", "Sync the user role attribute from Salesforce to the Okta Universal Directory",
             expected_resource_type="okta_user_profile_mapping"),
    TestCase("PM03", "Create a profile mapping that pushes the manager field from Okta to the HR portal app",
             expected_resource_type="okta_user_profile_mapping",
             must_contain=["okta_user_profile_mapping"]),
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
             must_contain=["okta_auth_server_scope"]),
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
             must_contain=["okta_factor", "DUO"],
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
             must_contain=["okta_auth_server", "okta_auth_server_scope", "okta_auth_server_policy"]),
    TestCase("COMP07",
             "Create a SAML app for Workday and map the costCenter and department attributes from Workday to the Okta user profile",
             expected_resource_type="okta_app_saml",
             must_contain=["okta_app_saml", "okta_user_profile_mapping"]),
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
        if "events_filter" not in non_comment_okta_hcl:
            issues.append("okta_event_hook missing 'events_filter' block")

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
        "okta_factor":              ["provider_id", "status"],
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
        "okta_factor":              [r"\bfactor_type\s*=", r"\bpolicy_id\s*="],
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
        if 'required_providers' not in gcp_hcl:
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
    if intent.get("operation_type") == "unknown" and (tc.gcp_types or tc.aws_types or tc.okta_types):
        intent["operation_type"] = "create"
    if tc.okta_types:
        intent["resource_types"] = tc.okta_types
    if tc.aws_types:
        intent["aws_resource_types"] = tc.aws_types
    if tc.gcp_types:
        intent["gcp_resource_types"] = tc.gcp_types
    # Mode mapping mirrors app.py:Stage 1 (after-parse):
    # both okta+gcp → "Okta + GCP", gcp alone → "GCP only", okta+aws → "Both",
    # okta alone → "Okta Terraform only", aws alone (rare) → "Lambda only".
    if tc.gcp_types and tc.okta_types:
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


def main():
    argv = sys.argv[1:]
    replay_mode = "--replay" in argv
    passes, skip_indices = _parse_passes(argv)
    filter_ids = set(
        a.upper() for i, a in enumerate(argv)
        if not a.startswith("--") and i not in skip_indices
    )
    cases = [tc for tc in TEST_CASES if not filter_ids or tc.id.upper() in filter_ids]

    if replay_mode:
        client = None
        model = None
        print(f"QA runner — REPLAY MODE — {len(cases)} tests — reading from qa_outputs_cache.json")
    else:
        api_key = _read_api_key()
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY not found. Set it in the environment or .streamlit/secrets.toml")
            sys.exit(1)
        client = _wrap_client_for_usage_tracking(anthropic.Anthropic(api_key=api_key))
        model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
        passes_label = f" — passes: {passes}" if passes > 1 else ""
        print(f"QA runner — {len(cases)} tests — model: {model}{passes_label}")
    print("=" * 72)

    results = []
    passed = failed = errored = 0

    for i, tc in enumerate(cases, 1):
        label = f"[{i:02d}/{len(cases)}] {tc.id:<6} {tc.prompt[:55]:<55}"
        print(f"{label} ...", end="", flush=True)
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
