import json
import anthropic
from .prompts import GENERATOR_SYSTEM_PROMPT, GENERATOR_USER_PROMPT_TEMPLATE
from .parser import _extract_json
from .okta_brand_sanitizer import sanitize_okta_brand_refs
from .okta_app_scim_sanitizer import sanitize_okta_app_scim_refs
from .hcl_utils import merge_terraform_blocks

REQUIRED_OUTPUT_KEYS = {"terraform_okta_hcl", "terraform_lambda_hcl", "lambda_python", "lambda_requirements"}
OPTIONAL_OUTPUT_KEYS_WITH_DEFAULTS = {
    "terraform_gcp_hcl": "",
    "cloud_function_python": "",
    "cloud_function_requirements": "",
}

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
    for key, default in OPTIONAL_OUTPUT_KEYS_WITH_DEFAULTS.items():
        if key not in parsed or not isinstance(parsed.get(key), str):
            parsed[key] = default
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
    gcp_types = intent.get("gcp_resource_types", [])
    if gcp_types:
        gcp_resource_section = (
            "GCP resources to include in terraform_gcp_hcl (in addition to the standard "
            f"Cloud Function + service account + source bucket): {', '.join(gcp_types)}. "
            "Follow the rules for each in SECTION C2."
        )
    else:
        gcp_resource_section = ""
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
        gcp_resource_section=gcp_resource_section,
    )
    messages = [{"role": "user", "content": user_content}]

    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=[
            {
                "type": "text",
                "text": GENERATOR_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
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
                "content": "Your response was not valid JSON. Return only the JSON object with the required keys (terraform_okta_hcl, terraform_lambda_hcl, terraform_gcp_hcl, lambda_python, lambda_requirements, cloud_function_python, cloud_function_requirements, terraform_tfvars_example), no other text.",
            },
        ]
        retry_response = client.messages.create(
            model=model,
            max_tokens=8192,
            system=[
                {
                    "type": "text",
                    "text": GENERATOR_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
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
        result["terraform_gcp_hcl"] = ""
        result["lambda_python"] = ""
        result["lambda_requirements"] = ""
        result["cloud_function_python"] = ""
        result["cloud_function_requirements"] = ""
        result["optional_tf"] = ""
    elif output_mode == "Lambda only":
        result["terraform_okta_hcl"] = ""
        result["terraform_gcp_hcl"] = ""
        result["cloud_function_python"] = ""
        result["cloud_function_requirements"] = ""
        result["optional_tf"] = ""
    elif output_mode == "GCP only":
        result["terraform_okta_hcl"] = ""
        result["terraform_lambda_hcl"] = ""
        result["lambda_python"] = ""
        result["lambda_requirements"] = ""
        result["optional_tf"] = ""
    elif output_mode == "Okta + GCP":
        result["terraform_lambda_hcl"] = ""
        result["lambda_python"] = ""
        result["lambda_requirements"] = ""
    elif output_mode == "Both":
        # "Both" means Okta + AWS Lambda — explicitly NOT GCP
        result["terraform_gcp_hcl"] = ""
        result["cloud_function_python"] = ""
        result["cloud_function_requirements"] = ""

    # Strip forbidden okta_brand attributes (logo, primary_color, secondary_color)
    # — provider v4.x does not support them and apply fails. Runs in every
    # generate_all caller, including qa_runner, not just app.py's refinement loop.
    result = sanitize_okta_brand_refs(result)

    # Strip hallucinated SCIM/provisioning blocks from okta_app_saml /
    # okta_app_oauth resources; provider v4.x has no SCIM support; SCIM is
    # UI-only. Inserts a NOTE comment pointing to the Admin Console.
    result = sanitize_okta_app_scim_refs(result)

    # In Okta + GCP composite mode, both terraform_okta_hcl and
    # terraform_gcp_hcl independently emit a `terraform { required_providers {} }`
    # block. When the user saves them as two .tf files in the same module,
    # terraform init fails with "Duplicate required providers configuration".
    # Merge the gcp required_providers entries into okta and strip the
    # terraform block from gcp.
    if output_mode == "Okta + GCP":
        merged_okta, merged_gcp = merge_terraform_blocks(
            result.get("terraform_okta_hcl", ""),
            result.get("terraform_gcp_hcl", ""),
        )
        result["terraform_okta_hcl"] = merged_okta
        result["terraform_gcp_hcl"] = merged_gcp

    return result
