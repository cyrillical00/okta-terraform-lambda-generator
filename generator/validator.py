import json
import anthropic
from .parser import _extract_json
from .terraform_gen import GenerationError

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

Do NOT flag:
- Style preferences or minor formatting choices
- The presence of Lambda code when the user selected "Okta Terraform only" — output_mode is a display filter, not a code correctness issue; Lambda is always generated regardless of display mode
- Variables declared for future use
- Inline comments explaining design decisions
- For okta_event_hook: do not flag that "no Lambda endpoint exists" — the Lambda and its aws_lambda_function_url live in terraform_lambda_hcl which you are not shown; the event_hook_url variable is correctly left as a var.* for the user to fill in after deployment
- For okta_event_hook: do not flag the absence of okta_app_group_assignment, okta_app_user_assignment, or similar Okta-side assignment resources when the intent is clearly event-driven (Lambda handles the API calls at runtime)
- Variable declarations without a corresponding data source lookup — validating a var.* value at apply time is the user's responsibility, not a code error

Only flag things that are technically wrong, produce incorrect behavior, or would cause terraform apply to fail or the Lambda to misbehave at runtime."""

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


def refine_outputs(
    intent: dict,
    outputs: dict,
    user_input: str,
    client: anthropic.Anthropic,
    model: str,
    max_passes: int = 3,
    on_pass: callable = None,
) -> dict:
    """Validate and auto-fix outputs up to max_passes times. Returns best-effort result."""
    for pass_num in range(1, max_passes + 1):
        result = validate_outputs(user_input, intent, outputs, client, model)
        has_issues = bool(result.get("terraform_issues") or result.get("lambda_issues"))
        if on_pass:
            on_pass(pass_num, result, has_issues)
        if result["overall"] == "pass" or not has_issues:
            break
        try:
            optional_tf = outputs.get("optional_tf", "")
            tfvars = outputs.get("terraform_tfvars_example", "")
            outputs = fix_outputs(intent, outputs, result, client, model)
            if optional_tf and not outputs.get("optional_tf"):
                outputs["optional_tf"] = optional_tf
            if tfvars and not outputs.get("terraform_tfvars_example"):
                outputs["terraform_tfvars_example"] = tfvars
        except GenerationError:
            break
    return outputs


FIXER_SYSTEM_PROMPT = """You are an expert Okta Terraform and AWS Lambda engineer. You will be given:
1. The confirmed intent (what was requested)
2. The current generated outputs (Terraform HCL and Lambda Python)
3. A list of specific issues found by an independent reviewer

Your job is to fix ONLY the listed issues. Do not change anything that is not broken.

Return ONLY a JSON object with exactly these four keys:
{
  "terraform_okta_hcl": "...",
  "terraform_lambda_hcl": "...",
  "lambda_python": "...",
  "lambda_requirements": "..."
}

## Canonical okta_event_hook schema (use this EXACTLY when fixing event hook issues)

Only these five top-level attributes are valid for okta_event_hook: name, status, channel, events_filter, headers.
NEVER use: events, filters, auth_type, url, eventFilters, or any other attribute name.

```hcl
resource "okta_event_hook" "example" {
  name   = "Hook Name"
  status = "ACTIVE"

  channel = {
    version = "1.0.0"
    uri     = var.event_hook_url
    type    = "HTTP"
  }

  events_filter = {
    type  = "EVENT_TYPE"
    items = ["group.user_membership.add"]
  }

  headers = [{
    key   = "Authorization"
    value = "Bearer ${var.event_hook_auth_token}"
  }]
}
```

When fixing an event hook, also ensure terraform_lambda_hcl contains:
```hcl
resource "aws_lambda_function_url" "handler" {
  function_name      = aws_lambda_function.handler.function_name
  authorization_type = "NONE"
}

output "lambda_function_url" {
  value       = aws_lambda_function_url.handler.function_url
  description = "Paste this URL into var.event_hook_url"
}
```

## General fix rules
- Fix every issue listed. Do not leave any unfixed.
- Do not add resources, attributes, or logic that was not in the original intent.
- Do not remove resources that are correct and intentional.
- Keep all var.* references for credentials — never hardcode values.
- If a Lambda issue says event hook boilerplate is wrong, remove it and replace with simple logging/processing logic appropriate for the resource type.
- Return complete, valid HCL and Python — not snippets or diffs.
- No markdown fences. No prose. Only the JSON object."""

FIXER_USER_TEMPLATE = """Fix the following issues in the generated outputs.

## Confirmed intent
{intent_json}

## Current terraform_okta_hcl
{terraform_okta_hcl}

## Current terraform_lambda_hcl
{terraform_lambda_hcl}

## Current lambda_python
{lambda_python}

## Current lambda_requirements
{lambda_requirements}

## Issues to fix
Terraform issues:
{terraform_issues}

Lambda issues:
{lambda_issues}

Return only the corrected JSON object with all four keys."""


def fix_outputs(
    intent: dict,
    outputs: dict,
    validation_result: dict,
    client: anthropic.Anthropic,
    model: str,
) -> dict:
    tf_issues = validation_result.get("terraform_issues", [])
    lambda_issues = validation_result.get("lambda_issues", [])

    tf_issues_text = "\n".join(f"- {i}" for i in tf_issues) if tf_issues else "None"
    lambda_issues_text = "\n".join(f"- {i}" for i in lambda_issues) if lambda_issues else "None"

    user_content = FIXER_USER_TEMPLATE.format(
        intent_json=json.dumps({k: v for k, v in intent.items() if k != "answers"}, indent=2),
        terraform_okta_hcl=outputs.get("terraform_okta_hcl", ""),
        terraform_lambda_hcl=outputs.get("terraform_lambda_hcl", ""),
        lambda_python=outputs.get("lambda_python", ""),
        lambda_requirements=outputs.get("lambda_requirements", ""),
        terraform_issues=tf_issues_text,
        lambda_issues=lambda_issues_text,
    )

    messages = [{"role": "user", "content": user_content}]

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=FIXER_SYSTEM_PROMPT,
        messages=messages,
    )

    raw = _extract_json(response.content[0].text)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # One retry with assistant-turn injection
        messages.append({"role": "assistant", "content": response.content[0].text})
        messages.append({"role": "user", "content": "Your response was not valid JSON. Return only the JSON object with the four required keys, no other text."})
        retry = client.messages.create(
            model=model,
            max_tokens=4096,
            system=FIXER_SYSTEM_PROMPT,
            messages=messages,
        )
        raw = _extract_json(retry.content[0].text)
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            raise GenerationError("Fix returned unparseable JSON.", raw_response=retry.content[0].text)

    required = {"terraform_okta_hcl", "terraform_lambda_hcl", "lambda_python", "lambda_requirements"}
    missing = required - result.keys()
    if missing:
        raise GenerationError(f"Fix response missing keys: {missing}", raw_response=raw)

    return result
