import json
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


def parse_intent(user_input: str, client: anthropic.Anthropic, model: str = MODEL) -> dict:
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=INTENT_PARSER_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": INTENT_USER_PROMPT_TEMPLATE.format(user_input=user_input),
            }
        ],
    )
    raw = _extract_json(response.content[0].text)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Intent parsing failed: Claude returned non-JSON. Raw response: {raw[:500]}") from e


def validate_intent(intent: dict) -> list[str]:
    errors = []
    missing = REQUIRED_KEYS - set(intent.keys())
    if missing:
        errors.append(f"Missing required fields: {', '.join(sorted(missing))}")
        return errors
    if intent["operation_type"] not in ALLOWED_OPERATION_TYPES:
        errors.append(f"operation_type '{intent['operation_type']}' is not valid. Must be one of: {', '.join(sorted(ALLOWED_OPERATION_TYPES))}")
    if intent["resource_type"] not in ALLOWED_RESOURCE_TYPES:
        errors.append(f"resource_type '{intent['resource_type']}' is not valid. Must be one of: {', '.join(sorted(ALLOWED_RESOURCE_TYPES))}")
    if not isinstance(intent.get("attributes"), dict):
        errors.append("'attributes' must be a dict")
    if not isinstance(intent.get("ambiguities"), list):
        errors.append("'ambiguities' must be a list")
    if not isinstance(intent.get("notes"), list):
        errors.append("'notes' must be a list")
    return errors
