import json
import anthropic
from .parser import _extract_json

VALIDATOR_SYSTEM_PROMPT = """You are an independent code reviewer for Okta Terraform and AWS Lambda configurations.

You will be given:
1. The original user request (plain English)
2. The confirmed intent (structured JSON)
3. The generated Terraform HCL and Lambda Python

Your job is to check whether the generated outputs correctly match what was requested. You are a SEPARATE reviewer — not the system that generated the code.

Return ONLY a JSON object with this exact structure:

{
  "terraform_issues": [],
  "lambda_issues": [],
  "overall": "pass"
}

Rules:
- "terraform_issues" is a list of strings describing problems found in the Terraform output. Empty list if none.
- "lambda_issues" is a list of strings describing problems found in the Lambda output. Empty list if none.
- "overall" must be one of: "pass", "warn", "fail"
  - "pass" = no issues found
  - "warn" = minor issues that don't break functionality
  - "fail" = serious issues that would cause terraform apply to fail or Lambda to misbehave

Check for these specific problems in Terraform:
- Extra resources not present in the intent (e.g. okta_group_rule added when only okta_app_saml was requested)
- Self-referential depends_on (resource depending on itself)
- group_assignments referencing app IDs instead of group IDs
- Hardcoded credential values instead of var.* references
- Invalid or non-existent attribute references on resource types
- app_settings_json with null/placeholder values not required for this resource type
- Missing required arguments for the resource type

Check for these specific problems in Lambda:
- Event hook verification logic (x-okta-verification-challenge) present when resource_type is NOT okta_event_hook
- Lambda does not match the resource type in any meaningful way
- Syntax errors or obvious runtime errors

Do not flag style preferences or minor formatting choices. Only flag things that are wrong, misleading, or would cause failures."""

VALIDATOR_USER_TEMPLATE = """Review the following generated outputs.

## Original user request
{user_input}

## Confirmed intent
{intent_json}

## Generated terraform_okta_hcl
{terraform_okta_hcl}

## Generated lambda_python
{lambda_python}

Return only the JSON review object."""


def validate_outputs(
    user_input: str,
    intent: dict,
    outputs: dict,
    client: anthropic.Anthropic,
    model: str,
) -> dict:
    user_content = VALIDATOR_USER_TEMPLATE.format(
        user_input=user_input,
        intent_json=json.dumps({k: v for k, v in intent.items() if k != "answers"}, indent=2),
        terraform_okta_hcl=outputs.get("terraform_okta_hcl", ""),
        lambda_python=outputs.get("lambda_python", ""),
    )

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=VALIDATOR_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )

    raw = _extract_json(response.content[0].text)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "terraform_issues": ["Validator returned unparseable response."],
            "lambda_issues": [],
            "overall": "warn",
        }

    result.setdefault("terraform_issues", [])
    result.setdefault("lambda_issues", [])
    result.setdefault("overall", "warn")
    return result
