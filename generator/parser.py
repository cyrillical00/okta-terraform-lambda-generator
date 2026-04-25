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


def parse_intent(user_input: str, client: anthropic.Anthropic, model: str = MODEL, resource_type_hints: list[str] | None = None) -> dict:
    hint_section = ""
    if resource_type_hints:
        hint_section = f"\n\nResource types explicitly selected by the user: {', '.join(resource_type_hints)}. Use these to inform resource_type selection — prefer one of these types over guessing."
    response = client.messages.create(
        model=model,
        max_tokens=1024,
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
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Intent parsing failed: Claude returned non-JSON. Raw response: {raw[:500]}") from e


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
