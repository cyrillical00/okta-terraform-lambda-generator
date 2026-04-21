import json
import anthropic
from .prompts import GENERATOR_SYSTEM_PROMPT, GENERATOR_USER_PROMPT_TEMPLATE
from .parser import _extract_json

REQUIRED_OUTPUT_KEYS = {"terraform_okta_hcl", "terraform_lambda_hcl", "lambda_python", "lambda_requirements"}

MODEL = "claude-haiku-4-5-20251001"


class GenerationError(Exception):
    def __init__(self, message: str, raw_response: str = ""):
        super().__init__(message)
        self.raw_response = raw_response


def _parse_output(raw: str) -> dict:
    raw = _extract_json(raw)
    parsed = json.loads(raw)
    missing = REQUIRED_OUTPUT_KEYS - set(parsed.keys())
    if missing:
        raise ValueError(f"Generated output missing required keys: {', '.join(sorted(missing))}")
    if "optional_tf" in parsed and not isinstance(parsed["optional_tf"], str):
        parsed["optional_tf"] = ""
    if "terraform_tfvars_example" in parsed and not isinstance(parsed["terraform_tfvars_example"], str):
        parsed["terraform_tfvars_example"] = ""
    return parsed


def _format_clarifications(answers: dict) -> str:
    filled = {q: a for q, a in answers.items() if a.strip()}
    if not filled:
        return ""
    lines = ["User clarifications:"]
    for q, a in filled.items():
        lines.append(f"Q: {q}")
        lines.append(f"A: {a}")
    return "\n".join(lines) + "\n\n"


def generate_all(
    intent: dict,
    extra_instructions: str,
    client: anthropic.Anthropic,
    model: str = MODEL,
    env_context_section: str = "",
    provider_version: str = "~> 4.0",
    repo_context_section: str = "",
) -> dict:
    answers = intent.get("answers", {})
    output_mode = intent.get("output_mode", "Both")
    resource_types = intent.get("resource_types", [])
    if len(resource_types) > 1:
        multi_resource_section = (
            f"MULTI-RESOURCE: Generate terraform_okta_hcl with complete resource blocks for ALL of these "
            f"Okta resource types: {', '.join(resource_types)}. Apply every individual resource type rule "
            "for each. All resources belong in a single terraform_okta_hcl string."
        )
    else:
        multi_resource_section = ""
    aws_types = intent.get("aws_resource_types", [])
    if aws_types:
        aws_resource_section = (
            "AWS resources to include in terraform_lambda_hcl (in addition to the standard "
            f"Lambda + IAM role): {', '.join(aws_types)}. Follow the rules for each in Section B."
        )
    else:
        aws_resource_section = ""
    user_content = GENERATOR_USER_PROMPT_TEMPLATE.format(
        intent_json=json.dumps({k: v for k, v in intent.items() if k not in ("answers", "output_mode", "provider_version")}, indent=2),
        output_mode=output_mode,
        clarifications_section=_format_clarifications(answers),
        extra_instructions=extra_instructions or "None",
        env_context_section=env_context_section,
        provider_version=provider_version,
        repo_context_section=repo_context_section,
        multi_resource_section=multi_resource_section,
        aws_resource_section=aws_resource_section,
    )
    messages = [{"role": "user", "content": user_content}]

    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=GENERATOR_SYSTEM_PROMPT,
        messages=messages,
    )
    raw = response.content[0].text.strip()

    try:
        result = _parse_output(raw)
    except (json.JSONDecodeError, ValueError):
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": "Your response was not valid JSON. Return only the JSON object with the required keys (terraform_okta_hcl, terraform_lambda_hcl, lambda_python, lambda_requirements, terraform_tfvars_example), no other text.",
            },
        ]
        retry_response = client.messages.create(
            model=model,
            max_tokens=8192,
            system=GENERATOR_SYSTEM_PROMPT,
            messages=retry_messages,
        )
        retry_raw = retry_response.content[0].text.strip()
        try:
            result = _parse_output(retry_raw)
        except (json.JSONDecodeError, ValueError) as e:
            raise GenerationError(
                f"Generation failed after retry: {e}",
                raw_response=retry_raw,
            ) from e

    # Hard-enforce output_mode constraints in code — prompt alone is not reliable enough.
    if output_mode == "Okta Terraform only":
        result["terraform_lambda_hcl"] = ""
        result["lambda_python"] = ""
        result["lambda_requirements"] = ""
        result["optional_tf"] = ""
    elif output_mode == "Lambda only":
        result["terraform_okta_hcl"] = ""
        result["optional_tf"] = ""

    return result
