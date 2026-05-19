"""Framework-agnostic orchestration for the TF Tool generate pipeline.

This module extracts the non-UI portion of the Streamlit app's flow so the
same code path can be driven from a CLI, HTTP API, Slack bot, JIRA webhook,
or anything else that holds an Anthropic client. There is no Streamlit
import here; callers pass everything explicitly (env context, repo context,
output mode, callbacks) and receive a plain dataclass back.

Two entry points:

- generate(prompt, ...): full flow, parses the user prompt into an intent,
  then runs generate -> refine -> sanitize. Use this from CLI / API / Slack
  callers that start with a free-text request.

- generate_from_intent(intent, ...): skips the parse step and runs the
  generate -> refine -> sanitize tail. Use this from app.py, which already
  parses the intent earlier (so the user can confirm or edit it via the
  intent card before generation begins).

Both return a GenerateResult with intent, outputs, and either an error
string or a cancelled flag. Callers handle rendering, audit logging, and
session persistence themselves; this module is pure orchestration.
"""

from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

from generator.parser import parse_intent, validate_intent
from generator.terraform_gen import generate_all, GenerationError
from generator.validator import refine_outputs
from generator.okta_group_sanitizer import sanitize_okta_group_refs
from env_context import format_context_for_prompt
from repo_context import format_repo_context_for_prompt
import structured_log


class GenerationCancelled(Exception):
    """Raised by the per-pass callback when the caller's cancel_check
    returns True. Caught inside generate / generate_from_intent so a
    cancellation surfaces as GenerateResult(cancelled=True) rather than an
    unhandled traceback. Callers may also catch this directly if they want
    to distinguish cancellation from a successful return.
    """
    pass


@dataclass
class GenerateResult:
    """Result of a generate pipeline call.

    Exactly one of (outputs is not None), (error is not None), or
    (cancelled is True) will be set on a successful return path. The
    intent field is always populated when the parse step succeeded; it is
    an empty dict only when the parse itself failed.
    """
    intent: dict
    outputs: dict | None = None
    validation_result: dict | None = None
    cancelled: bool = False
    error: str | None = None
    error_raw_response: str = ""


def _coerce_intent_resource_types(intent: dict) -> None:
    """Mirror app.py's behaviour of guaranteeing intent['resource_types']
    is a list. The Streamlit app does this in its parse stage; we do it
    here so CLI / API callers get the same shape. Mutates in place."""
    if not intent.get("resource_types"):
        intent["resource_types"] = [intent.get("resource_type", "")]


def generate_from_intent(
    intent: dict,
    *,
    client: anthropic.Anthropic,
    model: str = "claude-haiku-4-5-20251001",
    user_input: str = "",
    output_mode: str | None = None,
    provider_version: str | None = None,
    env_context: dict | None = None,
    repo_context_files: dict[str, str] | None = None,
    extra_instructions: str = "",
    max_passes: int = 3,
    on_pass: Callable[[int, dict, bool], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    on_text_delta: Callable[[str], None] | None = None,
) -> GenerateResult:
    """Run the generate -> refine -> sanitize tail of the pipeline against
    a pre-parsed intent dict.

    Behaviour mirrors app.py:_generate_and_refine exactly:
    1. generate_all with formatted env / repo context sections
    2. refine_outputs with the supplied on_pass callback
    3. sanitize_okta_group_refs against the live okta groups list

    The cancel_check callable, if provided, is consulted between refinement
    passes (inside the on_pass wrapper). It cannot interrupt an in-flight
    LLM call; that constraint is unchanged from the Streamlit version.

    output_mode and provider_version default to the values inside the
    intent dict if the caller does not pass them explicitly. This lets
    app.py keep its current call shape while CLI / API callers can
    override either independently.
    """
    if output_mode is None:
        output_mode = intent.get("output_mode", "Both")
    if provider_version is None:
        provider_version = intent.get("provider_version", "~> 4.0")

    env_section = format_context_for_prompt(env_context or {})
    repo_section = format_repo_context_for_prompt(repo_context_files or {})

    def _wrapped_on_pass(pass_num: int, result: dict, has_issues: bool) -> None:
        if cancel_check is not None and cancel_check():
            raise GenerationCancelled(f"cancelled before pass {pass_num}")
        if on_pass is not None:
            on_pass(pass_num, result, has_issues)

    try:
        outputs = generate_all(
            intent,
            extra_instructions,
            client,
            model=model,
            env_context_section=env_section,
            provider_version=provider_version,
            repo_context_section=repo_section,
            on_text_delta=on_text_delta,
        )
        structured_log.log_info(
            "generate_first_pass_complete",
            resource_type=intent.get("resource_type"),
            output_mode=output_mode,
            provider_version=provider_version,
            output_keys=sorted([k for k, v in outputs.items() if v]),
        )
        outputs = refine_outputs(
            intent=intent,
            outputs=outputs,
            user_input=user_input,
            client=client,
            model=model,
            max_passes=max_passes,
            on_pass=_wrapped_on_pass,
            output_mode=output_mode,
            on_text_delta=on_text_delta,
        )
        structured_log.log_info(
            "generate_refined",
            resource_type=intent.get("resource_type"),
            output_mode=output_mode,
            max_passes=max_passes,
        )
        live_groups = (env_context or {}).get("okta", {}).get("groups") or []
        outputs = sanitize_okta_group_refs(outputs, live_groups)
        structured_log.log_info(
            "generate_complete",
            resource_type=intent.get("resource_type"),
            output_mode=output_mode,
            live_groups_count=len(live_groups),
        )
        return GenerateResult(intent=intent, outputs=outputs)
    except GenerationCancelled:
        structured_log.log_warn(
            "generate_cancelled",
            resource_type=intent.get("resource_type"),
            output_mode=output_mode,
        )
        return GenerateResult(intent=intent, cancelled=True)
    except GenerationError as e:
        structured_log.log_error(
            "generate_failed",
            resource_type=intent.get("resource_type"),
            output_mode=output_mode,
            error=str(e),
        )
        return GenerateResult(
            intent=intent,
            error=str(e),
            error_raw_response=getattr(e, "raw_response", ""),
        )


def generate(
    prompt: str,
    *,
    client: anthropic.Anthropic,
    model: str = "claude-haiku-4-5-20251001",
    output_mode: str = "Both",
    provider_version: str = "~> 4.0",
    env_context: dict | None = None,
    repo_context_files: dict[str, str] | None = None,
    extra_instructions: str = "",
    max_passes: int = 3,
    resource_type_hints: list[str] | None = None,
    on_pass: Callable[[int, dict, bool], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> GenerateResult:
    """Full pipeline starting from a free-text prompt.

    Use this from CLI, HTTP, Slack, or JIRA callers that have no UI
    intent-confirmation step. Steps:

    1. parse_intent -> structured intent dict
    2. validate_intent; on validation errors, return GenerateResult with
       error set and outputs=None.
    3. Stamp output_mode and provider_version onto the intent so the
       downstream generator sees them.
    4. Delegate to generate_from_intent for the rest of the flow.

    The Streamlit app does NOT use this function; it parses earlier and
    calls generate_from_intent directly so the user can confirm or edit
    the intent before generation runs.
    """
    try:
        intent = parse_intent(prompt, client, model=model, resource_type_hints=resource_type_hints)
    except ValueError as e:
        structured_log.log_error("generate_parse_failed", error=str(e))
        return GenerateResult(intent={}, error=str(e))

    errors = validate_intent(intent)
    if errors:
        structured_log.log_warn(
            "generate_validation_failed",
            resource_type=intent.get("resource_type"),
            errors=errors,
        )
        return GenerateResult(
            intent=intent,
            error="Validation errors: " + "; ".join(errors),
        )
    structured_log.log_info(
        "generate_parsed",
        resource_type=intent.get("resource_type"),
        operation_type=intent.get("operation_type"),
        output_mode=output_mode,
    )

    _coerce_intent_resource_types(intent)
    intent["output_mode"] = output_mode
    intent["provider_version"] = provider_version

    return generate_from_intent(
        intent,
        client=client,
        model=model,
        user_input=prompt,
        output_mode=output_mode,
        provider_version=provider_version,
        env_context=env_context,
        repo_context_files=repo_context_files,
        extra_instructions=extra_instructions,
        max_passes=max_passes,
        on_pass=on_pass,
        cancel_check=cancel_check,
    )
