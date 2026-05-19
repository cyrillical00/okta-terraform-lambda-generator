import json
from difflib import get_close_matches

import anthropic
from .prompts import INTENT_PARSER_SYSTEM_PROMPT, INTENT_USER_PROMPT_TEMPLATE

ALLOWED_OPERATION_TYPES = {"create", "update", "delete", "import"}
ALLOWED_RESOURCE_TYPES = {
    "okta_app_saml",
    "okta_app_oauth",
    "okta_group",
    "okta_group_rule",
    "okta_event_hook",
    "okta_user_profile_mapping",
    "okta_auth_server",
    "okta_auth_server_scope",
    "okta_auth_server_claim",
    "okta_auth_server_policy",
    "okta_auth_server_policy_rule",
    "okta_factor",
    "okta_network_zone",
    "okta_brand",
    "okta_email_customization",
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
    "fleet_policy",
    "fleet_label",
    "fleet_query",
    "fleet_configuration_profile",
    "fleet_script",
    "fleet_software_package",
    "fleet_agent_options",
    "fleet_team_settings",
    "snowflake_warehouse",
    "snowflake_database",
    "snowflake_schema",
    "snowflake_role",
    "snowflake_user",
    "snowflake_grant_account_role",
    "snowflake_grant_privileges_to_account_role",
    "snowflake_resource_monitor",
    "snowflake_network_policy",
    "snowflake_scim_integration",
    "iru_blueprint",
    "iru_blueprint_routing",
    "iru_blueprint_library_item",
    "iru_custom_script",
    "iru_custom_profile",
    "iru_custom_app",
    "iru_in_house_app",
    "iru_tag",
    "iru_device_note",
    "iru_ade_integration",
    "iru_ade_device",
    "unknown",
}
REQUIRED_KEYS = {"operation_type", "resource_type", "resource_name", "attributes", "notes", "ambiguities"}

MODEL = "claude-haiku-4-5-20251001"


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # drop opening fence line and closing fence
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner).strip()
    return text


def parse_intent(
    user_input: str,
    client: anthropic.Anthropic,
    model: str = MODEL,
    resource_type_hints: list[str] | None = None,
    on_text_delta: callable = None,
) -> dict:
    hint_section = ""
    if resource_type_hints:
        hint_section = f"\n\nResource types explicitly selected by the user: {', '.join(resource_type_hints)}. Use these to inform resource_type selection - prefer one of these types over guessing."
    from ._stream import streamed_create
    response = streamed_create(
        client,
        on_text_delta=on_text_delta,
        model=model,
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": INTENT_PARSER_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": INTENT_USER_PROMPT_TEMPLATE.format(user_input=user_input) + hint_section,
            }
        ],
    )
    raw = _extract_json(response.content[0].text)
    try:
        intent = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Intent parsing failed: Claude returned non-JSON. Raw response: {raw[:500]}") from e
    # Normalise the primary resource_type for compound requests. The LLM
    # classifier occasionally routes "Create an app AND an auth server with a
    # scope" to okta_auth_server_scope (the child) rather than okta_app_oauth
    # (the app), even though resource_types lists both. Deterministic
    # post-process to promote the app to primary when both an app type and
    # an auth-server child appear in resource_types.
    intent = _normalize_compound_primary(intent)
    # Detect enumerated multi-object prompts ("create three groups: A, B, C")
    # and attach the parsed instance list. Downstream the generator user-prompt
    # template surfaces this list explicitly and the multi-object sanitizer
    # cleans up if the LLM still emits a single block.
    from .multi_object_detector import detect_instances
    instances = detect_instances(user_input)
    if instances is not None:
        intent["instances"] = instances
    # Attach the raw user input so downstream sanitizers (event-hook event-type
    # mapping, etc.) can derive prompt-language signals without re-plumbing
    # signatures through generate_all.
    intent["user_input"] = user_input
    return intent


# Compound-prompt primary normalisation. When the user requests an app PLUS an
# auth-server child (scope, claim, policy, policy rule), the LLM sometimes
# returns the child as resource_type even though resource_types correctly
# lists the app. This pure post-process restores the app as primary so
# downstream generators dispatch correctly.
_COMPOUND_APP_TYPES = frozenset({"okta_app_oauth", "okta_app_saml"})
_COMPOUND_AUTH_CHILD_TYPES = frozenset({
    "okta_auth_server_scope",
    "okta_auth_server_claim",
    "okta_auth_server_policy",
    "okta_auth_server_policy_rule",
})


def _normalize_compound_primary(intent: dict) -> dict:
    """Promote the app type to primary when an app + auth-server child both
    appear in resource_types but the LLM picked the child as primary.

    Pure, idempotent. Mutates and returns the intent dict in place to keep
    parity with the existing parse_intent flow."""
    primary = intent.get("resource_type")
    if primary not in _COMPOUND_AUTH_CHILD_TYPES:
        return intent
    types = set(intent.get("resource_types") or [])
    app_types = types & _COMPOUND_APP_TYPES
    if not app_types:
        return intent
    # Deterministic order: oauth wins over saml if both are present (the
    # observed compound test case is oauth + scope; saml + scope is not in
    # the test corpus).
    if "okta_app_oauth" in app_types:
        intent["resource_type"] = "okta_app_oauth"
    else:
        intent["resource_type"] = "okta_app_saml"
    return intent


def _fuzzy_correct(value: str, valid: set[str], cutoff: float = 0.7) -> str:
    if value in valid:
        return value
    matches = get_close_matches(value, list(valid), n=1, cutoff=cutoff)
    return matches[0] if matches else value


def validate_intent(intent: dict) -> list[str]:
    errors = []
    missing = REQUIRED_KEYS - set(intent.keys())
    if missing:
        errors.append(f"Missing required fields: {', '.join(sorted(missing))}")
        return errors

    # Auto-correct near-misses before hard-failing
    op = _fuzzy_correct(intent["operation_type"], ALLOWED_OPERATION_TYPES)
    if op != intent["operation_type"]:
        intent["operation_type"] = op
    if intent["operation_type"] not in ALLOWED_OPERATION_TYPES:
        errors.append(f"operation_type '{intent['operation_type']}' is not valid. Must be one of: {', '.join(sorted(ALLOWED_OPERATION_TYPES))}")

    rt = _fuzzy_correct(intent["resource_type"], ALLOWED_RESOURCE_TYPES)
    if rt != intent["resource_type"]:
        intent["resource_type"] = rt
    if intent["resource_type"] not in ALLOWED_RESOURCE_TYPES:
        errors.append(f"resource_type '{intent['resource_type']}' is not valid. Must be one of: {', '.join(sorted(ALLOWED_RESOURCE_TYPES))}")

    if not isinstance(intent.get("attributes"), dict):
        errors.append("'attributes' must be a dict")
    if not isinstance(intent.get("ambiguities"), list):
        errors.append("'ambiguities' must be a list")
    if not isinstance(intent.get("notes"), list):
        errors.append("'notes' must be a list")

    if "resource_types" in intent:
        if not isinstance(intent["resource_types"], list):
            errors.append("'resource_types' must be a list")
        else:
            corrected = []
            invalid = []
            for rt in intent["resource_types"]:
                fixed = _fuzzy_correct(rt, ALLOWED_RESOURCE_TYPES)
                corrected.append(fixed)
                if fixed not in ALLOWED_RESOURCE_TYPES:
                    invalid.append(rt)
            intent["resource_types"] = corrected
            if invalid:
                errors.append(f"resource_types contains invalid values: {', '.join(invalid)}")

    return errors
