import json
import anthropic
from .prompts import GENERATOR_SYSTEM_PROMPT, GENERATOR_USER_PROMPT_TEMPLATE

REQUIRED_OUTPUT_KEYS = {"terraform_okta_hcl", "terraform_lambda_hcl", "lambda_python", "lambda_requirements"}


class GenerationError(Exception):
    def __init__(self, message: str, raw_response: str = ""):
        super().__init__(message)
        self.raw_response = raw_response


def _parse_output(raw: str) -> dict:
    parsed = json.loads(raw)
    missing = REQUIRED_OUTPUT_KEYS - set(parsed.keys())
    if missing:
        raise ValueError(f"Generated output missing required keys: {', '.join(sorted(missing))}")
    return parsed


def generate_all(intent: dict, extra_instructions: str, client: anthropic.Anthropic) -> dict:
    user_content = GENERATOR_USER_PROMPT_TEMPLATE.format(
        intent_json=json.dumps(intent, indent=2),
        extra_instructions=extra_instructions or "None",
    )
    messages = [{"role": "user", "content": user_content}]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
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
        return _parse_output(raw)
    except (json.JSONDecodeError, ValueError):
        # Assistant-turn injection retry: show Claude its own broken output and ask it to fix it
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": "Your response was not valid JSON. Return only the JSON object with the four required keys (terraform_okta_hcl, terraform_lambda_hcl, lambda_python, lambda_requirements), no other text.",
            },
        ]
        retry_response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
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
            return _parse_output(retry_raw)
        except (json.JSONDecodeError, ValueError) as e:
            raise GenerationError(
                f"Generation failed after retry: {e}",
                raw_response=retry_raw,
            ) from e
