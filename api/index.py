"""Vercel Python serverless entrypoint — FastAPI app exposing the TF Tool.

Routes (all namespaced under /api/* so a single Vercel function serves
every surface):

  GET  /api/health      liveness, no auth — for uptime checks
  POST /api/generate    X-API-Key required — full free-text -> files pipeline
  POST /api/push        X-API-Key required — push files to GitHub on a branch

Slack and JIRA handlers register their own routes onto this same `app`
object from `api/slack.py` and `api/jira.py`. Vercel routes everything
matching `/api/(.*)` to this single function (see vercel.json), so the
slash-command and webhook endpoints land here too.

Rationale for one function instead of one per route: cold-start cost is
paid once per worker instead of per route, and the cost-wrapped client +
configured audit modules are shared across all surfaces in a warm worker.

This module is import-safe — Vercel imports it once per cold start, then
reuses the same `app` for every request via Fluid Compute.
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import audit
import cost
import redact
import repo_context
import roles
from core import service as core_service
from gh_push.push import push_to_github
from headless_rate_limit import (
    HTTP_RATE_LIMITER,
    INPUT_LENGTH_CAP_BYTES,
    check_input_length,
)

from api import _push_policy
from api._auth import ApiKeyEntry, verify_api_caller
from api._bootstrap import USAGE_UNAVAILABLE_MESSAGE, prepare_client, quota_blocked


def _client_ip(request: Request) -> str:
    """Best-effort client IP for rate-limiter keying.

    Vercel sets `X-Forwarded-For` with the original caller's IP as the
    first comma-separated entry; we prefer that over `request.client.host`
    (which is the inbound TLS terminator, not the real caller). Falls
    back to "unknown" when neither is set so the limiter still has a
    stable key for tests / local runs.
    """
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        # First entry is the original client; subsequent entries are
        # intermediate proxies appended by each hop.
        first = xff.split(",")[0].strip()
        if first:
            return first
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _require(action: str, caller: ApiKeyEntry) -> None:
    """Enforce the role attached to the caller's API key.

    Uses the same capability matrix as the Streamlit app (roles.py), so a
    "viewer" key cannot generate and only "editor" and above can push.
    """
    if not roles.can(action, caller.role):
        audit.log(
            caller.actor_id,
            "http_forbidden",
            extra={"surface": "http", "action": action, "role": caller.role},
        )
        raise HTTPException(
            status_code=403,
            detail=f"this API key's role ({caller.role}) may not {action.replace('_', ' ')}",
        )


# ─── shared FastAPI app ───────────────────────────────────────────────────

app = FastAPI(
    title="TF Tool HTTP API",
    description="Generate Okta + AWS Lambda + GCP Terraform from plain English.",
    version="1.0.0",
)


# Side-effect: import slack/jira route modules so they register their
# own endpoints on the same `app`. Wrapped in try/except so a missing
# optional dep (e.g. slack_sdk not installed in a test env) doesn't
# break the core API.
try:
    from api import slack  # noqa: F401  (registers /api/slack/* routes)
except Exception:
    pass
try:
    from api import jira  # noqa: F401  (registers /api/jira/* routes)
except Exception:
    pass


# ─── valid output modes (must mirror cli.py / app.py / qa_runner.py) ─────

OutputMode = Literal[
    "Both",
    "Okta Terraform only",
    "Lambda only",
    "GCP only",
    "Okta + GCP",
    "JAMF only",
    "Okta + JAMF",
    "Fleet GitOps only",
    "Okta + Fleet GitOps",
    "Fleet TF only",
    "Okta + Fleet TF",
    "Snowflake only",
    "Okta + Snowflake",
]


# ─── limits on the optional generate inputs ──────────────────────────────
# The prompt has its own cap (INPUT_LENGTH_CAP_BYTES). Every other
# caller-supplied field reaches the model too, so each one is bounded and
# passed through the same redaction as the prompt.

EXTRA_INSTRUCTIONS_CAP_BYTES = INPUT_LENGTH_CAP_BYTES
ENV_CONTEXT_CAP_BYTES = 200_000
# Same ceilings the Streamlit repo importer applies (repo_context.py).
REPO_CONTEXT_MAX_FILES = repo_context.MAX_FILES
REPO_CONTEXT_CAP_BYTES = repo_context.MAX_TOTAL_CHARS
REPO_CONTEXT_PATH_MAX_CHARS = 200

# A Terraform version constraint: one or more comma-separated terms such as
# "~> 4.0" or ">= 4.0, < 5.0". Nothing else belongs in this field.
_VERSION_TERM = r"(?:(?:=|!=|>=|<=|>|<|~>)[ \t]*)?\d+(?:\.\d+){0,2}(?:-[0-9A-Za-z.]+)?"
PROVIDER_VERSION_PATTERN = rf"^[ \t]*{_VERSION_TERM}(?:[ \t]*,[ \t]*{_VERSION_TERM})*[ \t]*$"


def _extra_input_problem(body: "GenerateRequest") -> tuple[str, int] | None:
    """Return (reason, cap) for the first optional field over its limit."""
    ok, reason = check_input_length(body.extra_instructions, EXTRA_INSTRUCTIONS_CAP_BYTES)
    if not ok:
        return f"extra_instructions: {reason}", EXTRA_INSTRUCTIONS_CAP_BYTES

    if body.env_context is not None:
        size = len(json.dumps(body.env_context, default=str).encode("utf-8"))
        if size > ENV_CONTEXT_CAP_BYTES:
            return f"env_context: {size} bytes exceeds cap {ENV_CONTEXT_CAP_BYTES}", ENV_CONTEXT_CAP_BYTES

    files = body.repo_context_files or {}
    if len(files) > REPO_CONTEXT_MAX_FILES:
        return (
            f"repo_context_files: {len(files)} files exceeds limit {REPO_CONTEXT_MAX_FILES}",
            REPO_CONTEXT_CAP_BYTES,
        )
    if any(len(path) > REPO_CONTEXT_PATH_MAX_CHARS for path in files):
        return "repo_context_files: file path too long", REPO_CONTEXT_CAP_BYTES
    size = sum(len(path.encode("utf-8")) + len((text or "").encode("utf-8")) for path, text in files.items())
    if size > REPO_CONTEXT_CAP_BYTES:
        return f"repo_context_files: {size} bytes exceeds cap {REPO_CONTEXT_CAP_BYTES}", REPO_CONTEXT_CAP_BYTES
    return None


def _redact_tree(value: Any, summary: dict[str, int]) -> Any:
    """Redact every string inside a JSON-like value; counts go into `summary`.

    Dict keys (file paths, field names) are left as they are.
    """
    if isinstance(value, str):
        cleaned, found = redact.redact(value)
        for label, count in (found or {}).items():
            summary[label] = summary.get(label, 0) + count
        return cleaned
    if isinstance(value, dict):
        return {k: _redact_tree(v, summary) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_tree(v, summary) for v in value]
    return value


# ─── request / response shapes ───────────────────────────────────────────


class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="Plain-English description of the operation.")
    output_mode: OutputMode = "Both"
    provider_version: str = Field(
        default="~> 4.0",
        max_length=64,
        pattern=PROVIDER_VERSION_PATTERN,
        description="Okta provider version constraint, e.g. \"~> 4.0\" or \">= 4.0, < 5.0\".",
    )
    max_passes: int = Field(default=3, ge=1, le=5, description="Validate-and-fix passes.")
    extra_instructions: str = ""
    env_context: dict[str, Any] | None = Field(
        default=None,
        description="Optional pre-fetched environment context (Okta groups, etc.). Most HTTP callers skip this.",
    )
    repo_context_files: dict[str, str] | None = Field(
        default=None,
        description="Optional Terraform files from the target repo for context.",
    )


class GenerateResponse(BaseModel):
    intent: dict[str, Any]
    files: dict[str, str] | None = None
    validation_result: dict[str, Any] | None = None
    redact_summary: dict[str, int] | None = None
    cost_usd: float = 0.0
    cost_remaining_usd: float = 0.0
    error: str | None = None


class PushRequest(BaseModel):
    files: dict[str, str] = Field(..., min_length=1)
    repo: str = Field(..., description="owner/repo; must be on the server's push allow-list")
    branch: str = Field(
        default="",
        description="Target branch. Never the default branch; left empty, a tfgen/... branch is created.",
    )
    commit_message: str = Field(..., min_length=1)


class PushResponse(BaseModel):
    commit_url: str


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "tfgen-http"


# ─── routes ──────────────────────────────────────────────────────────────


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness probe. No auth — just confirms the function is responding.
    Does NOT verify ANTHROPIC_API_KEY or GITHUB_TOKEN; use /api/generate
    with a tiny prompt for that.
    """
    return HealthResponse()


@app.post("/api/generate", response_model=GenerateResponse)
def generate_endpoint(
    body: GenerateRequest,
    request: Request,
    caller: ApiKeyEntry = Depends(verify_api_caller),
) -> Any:
    """Full free-text -> Terraform pipeline.

    Flow mirrors app.py and cli.py:
      1. redact PII from the prompt
      2. quota check against today's spend
      3. cost-wrapped Anthropic client
      4. core.service.generate() parse + generate + refine + sanitize
      5. _build_files maps outputs to file paths (mirrors cli._build_file_map)
      6. audit.log the action

    Errors are surfaced as GenerateResponse.error rather than HTTP 5xx so
    clients can distinguish a generation failure (the model returned bad
    JSON, the prompt was unparseable) from an infrastructure failure
    (network, 500). Validation failures from Pydantic still come back as
    422; that's intended.

    Rate limiting and input-length capping run before quota check / LLM
    work so abusive callers cost zero LLM tokens. The 429 / 413 responses
    are JSONResponse instances rather than raised HTTPException so we can
    set a `Retry-After` header alongside the structured error body.
    """
    actor_id = caller.actor_id
    _require("generate", caller)
    client_ip = _client_ip(request)

    allowed, retry_after = HTTP_RATE_LIMITER.check(client_ip)
    if not allowed:
        audit.log(
            actor_id,
            "rate_limited_http",
            extra={
                "surface": "http",
                "client_ip": client_ip,
                "retry_after_seconds": retry_after,
            },
        )
        return JSONResponse(
            status_code=429,
            content={"error": "rate_limited", "retry_after_seconds": retry_after},
            headers={"Retry-After": str(retry_after)},
        )

    ok, reason = check_input_length(body.prompt, INPUT_LENGTH_CAP_BYTES)
    if not ok:
        audit.log(
            actor_id,
            "input_too_large_http",
            extra={
                "surface": "http",
                "client_ip": client_ip,
                "reason": reason,
                "cap_bytes": INPUT_LENGTH_CAP_BYTES,
            },
        )
        return JSONResponse(
            status_code=413,
            content={
                "error": "input_too_large",
                "reason": reason,
                "cap_bytes": INPUT_LENGTH_CAP_BYTES,
            },
        )

    problem = _extra_input_problem(body)
    if problem:
        reason, cap = problem
        audit.log(
            actor_id,
            "input_too_large_http",
            extra={"surface": "http", "client_ip": client_ip, "reason": reason, "cap_bytes": cap},
        )
        return JSONResponse(
            status_code=413,
            content={"error": "input_too_large", "reason": reason, "cap_bytes": cap},
        )

    ctx = prepare_client(actor_id)

    blocked, spent, quota = quota_blocked(ctx)
    if blocked and ctx.usage_unavailable:
        audit.log(
            actor_id, "quota_blocked",
            extra={"quota_usd": quota, "surface": "http", "reason": "usage_unavailable"},
        )
        raise HTTPException(status_code=503, detail=USAGE_UNAVAILABLE_MESSAGE)
    if blocked:
        audit.log(
            actor_id, "quota_blocked",
            extra={"spent_usd": spent, "quota_usd": quota, "surface": "http"},
        )
        raise HTTPException(
            status_code=429,
            detail=f"daily quota exhausted: ${spent:.2f} / ${quota:.2f} USD",
        )

    cleaned_prompt, redact_summary = redact.redact(body.prompt)
    # The optional fields go to the model as well: same redaction, one summary.
    redact_summary = dict(redact_summary or {})
    extra_instructions = _redact_tree(body.extra_instructions, redact_summary)
    env_context = _redact_tree(body.env_context, redact_summary)
    repo_context_files = _redact_tree(body.repo_context_files, redact_summary)

    result = core_service.generate(
        cleaned_prompt,
        client=ctx.client,
        model=ctx.model,
        output_mode=body.output_mode,
        provider_version=body.provider_version.strip(),
        max_passes=body.max_passes,
        extra_instructions=extra_instructions,
        env_context=env_context,
        repo_context_files=repo_context_files,
    )

    files = None
    if result.outputs and not result.error:
        files = _build_files(result.outputs, body.output_mode)

    # cost.wrap_client recorded usage as a side effect of every
    # client.messages.create() call inside core_service.generate(). Read
    # back today's total to compute the delta this request consumed.
    today_after = cost.today_usd(actor_id)

    audit.log(
        actor_id,
        "http_generate",
        resource_type=(result.intent or {}).get("resource_type", ""),
        output_mode=body.output_mode,
        cost_estimate_usd=max(0.0, today_after - spent),
        redacted_input_preview=cleaned_prompt[:200],
        extra={
            "surface": "http",
            "redact_summary": redact_summary,
            "passes": body.max_passes,
            "error": result.error or "",
        },
    )

    return GenerateResponse(
        intent=result.intent or {},
        files=files,
        validation_result=result.validation_result,
        redact_summary=redact_summary or None,
        cost_usd=max(0.0, today_after - spent),
        cost_remaining_usd=max(0.0, quota - today_after),
        error=result.error,
    )


@app.post("/api/push", response_model=PushResponse)
def push_endpoint(
    body: PushRequest,
    request: Request,
    caller: ApiKeyEntry = Depends(verify_api_caller),
) -> Any:
    """Push a file map to a GitHub repo on a branch.

    Uses the *server's* GITHUB_TOKEN, not the caller's. Because of that the
    caller does not get to choose freely where it writes:

      * the key's role must allow pushing (editor or admin)
      * the repo must be on TFGEN_PUSH_ALLOWED_REPOS (nothing is pushed
        while that list is empty)
      * paths must be inside the directories this tool generates into
      * the default branch is never written; an empty `branch` gets a
        generated `tfgen/...` name

    If a future use case needs caller-supplied tokens, add a
    `github_token` field to PushRequest and gate it behind a separate
    role.
    """
    actor_id = caller.actor_id
    _require("push_org", caller)

    client_ip = _client_ip(request)
    allowed, retry_after = HTTP_RATE_LIMITER.check(client_ip)
    if not allowed:
        audit.log(
            actor_id,
            "rate_limited_http",
            extra={
                "surface": "http",
                "route": "push",
                "client_ip": client_ip,
                "retry_after_seconds": retry_after,
            },
        )
        return JSONResponse(
            status_code=429,
            content={"error": "rate_limited", "retry_after_seconds": retry_after},
            headers={"Retry-After": str(retry_after)},
        )

    gh_token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if not gh_token:
        raise HTTPException(status_code=503, detail="server is not configured: GITHUB_TOKEN missing")
    if not _push_policy.allowed_repos():
        raise HTTPException(
            status_code=503,
            detail="server is not configured: TFGEN_PUSH_ALLOWED_REPOS is empty, so pushes are disabled",
        )

    try:
        repo = _push_policy.check_repo(body.repo)
        _push_policy.check_files(body.files)
        branch = _push_policy.resolve_branch(body.branch, actor_id)
        commit_message = _push_policy.check_commit_message(body.commit_message)
    except _push_policy.PushPolicyError as e:
        audit.log(
            actor_id,
            "http_push_rejected",
            extra={"repo": body.repo[:200], "branch": body.branch[:200], "reason": str(e)},
        )
        status = 403 if "allow-list" in str(e) else 400
        raise HTTPException(status_code=status, detail=str(e))

    try:
        url = push_to_github(
            body.files,
            repo,
            gh_token,
            commit_message,
            branch=branch,
            allow_default_branch=False,
        )
    except RuntimeError as e:
        # push_to_github raises RuntimeError for human-readable failures
        # (repo not found, branch can't be created, empty repo, default
        # branch refused).
        audit.log(actor_id, "http_push_failed", extra={"repo": repo, "branch": branch, "error": str(e)})
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001 — we want the audit log + 500
        # The detail goes to the audit log, not to the caller.
        audit.log(actor_id, "http_push_failed", extra={"repo": repo, "branch": branch, "error": str(e)})
        raise HTTPException(status_code=500, detail="push failed")

    audit.log(
        actor_id,
        "http_push",
        commit_url=url,
        extra={"repo": repo, "branch": branch, "files": len(body.files)},
    )

    return PushResponse(commit_url=url)


# ─── helpers ─────────────────────────────────────────────────────────────


def _build_files(outputs: dict[str, str], mode: str) -> dict[str, str]:
    """Map service outputs to filename -> content.

    Mirrors `cli._build_file_map` and `app.py:_build_files`. Kept as a
    private helper rather than imported from cli to avoid an api -> cli
    dependency direction (cli already imports from core; api imports
    from core too; neither should import the other).
    """
    files: dict[str, str] = {}
    if mode in ("Both", "Okta Terraform only", "Okta + GCP", "Okta + JAMF"):
        v = (outputs.get("terraform_okta_hcl") or "").strip()
        if v:
            files["terraform/okta.tf"] = v
    if mode == "Both":
        v = (outputs.get("terraform_lambda_hcl") or "").strip()
        if v:
            files["terraform/lambda.tf"] = v
    if mode in ("Both", "Lambda only"):
        v = (outputs.get("lambda_python") or "").strip()
        if v:
            files["lambda/lambda_function.py"] = v
        v = (outputs.get("lambda_requirements") or "").strip()
        if v:
            files["lambda/requirements.txt"] = v
    if mode in ("GCP only", "Okta + GCP"):
        v = (outputs.get("terraform_gcp_hcl") or "").strip()
        if v:
            files["terraform/gcp.tf"] = v
        v = (outputs.get("cloud_function_python") or "").strip()
        if v:
            files["cloud_function/main.py"] = v
        v = (outputs.get("cloud_function_requirements") or "").strip()
        if v:
            files["cloud_function/requirements.txt"] = v
    if mode in ("JAMF only", "Okta + JAMF"):
        v = (outputs.get("terraform_jamf_hcl") or "").strip()
        if v:
            files["terraform/jamf.tf"] = v
    v = (outputs.get("optional_tf") or "").strip()
    if v:
        files["terraform/optional_extensions.tf"] = v
    v = (outputs.get("terraform_tfvars_example") or "").strip()
    if v:
        files["terraform/terraform.tfvars.example"] = v
    return files
