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

import os
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

import audit
import cost
import redact
from core import service as core_service
from gh_push.push import push_to_github

from api._auth import verify_api_key
from api._bootstrap import prepare_client, quota_blocked


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
]


# ─── request / response shapes ───────────────────────────────────────────


class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="Plain-English description of the operation.")
    output_mode: OutputMode = "Both"
    provider_version: str = Field(default="~> 4.0", description="Okta provider version constraint.")
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
    repo: str = Field(..., description="owner/repo")
    branch: str = ""
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
    actor_id: str = Depends(verify_api_key),
) -> GenerateResponse:
    """Full free-text -> Terraform pipeline.

    Flow mirrors app.py and cli.py:
      1. redact PII from the prompt
      2. quota check against today's spend
      3. cost-wrapped Anthropic client
      4. core.service.generate() — parse + generate + refine + sanitize
      5. _build_files maps outputs to file paths (mirrors cli._build_file_map)
      6. audit.log the action

    Errors are surfaced as GenerateResponse.error rather than HTTP 5xx so
    clients can distinguish a generation failure (the model returned bad
    JSON, the prompt was unparseable) from an infrastructure failure
    (network, 500). Validation failures from Pydantic still come back as
    422 — that's intended.
    """
    ctx = prepare_client(actor_id)

    blocked, spent, quota = quota_blocked(ctx)
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

    result = core_service.generate(
        cleaned_prompt,
        client=ctx.client,
        model=ctx.model,
        output_mode=body.output_mode,
        provider_version=body.provider_version,
        max_passes=body.max_passes,
        extra_instructions=body.extra_instructions,
        env_context=body.env_context,
        repo_context_files=body.repo_context_files,
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
    actor_id: str = Depends(verify_api_key),
) -> PushResponse:
    """Push a file map to a GitHub repo on a branch.

    Uses the *server's* GITHUB_TOKEN, not the caller's. That's the same
    model as the Streamlit app: customers share the bot's identity for
    pushes. If a future use case needs caller-supplied tokens, add a
    `github_token` field to PushRequest and gate it behind a separate
    role.
    """
    gh_token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if not gh_token:
        raise HTTPException(status_code=503, detail="server is not configured: GITHUB_TOKEN missing")

    try:
        url = push_to_github(
            body.files,
            body.repo,
            gh_token,
            body.commit_message,
            branch=body.branch,
        )
    except RuntimeError as e:
        # push_to_github raises RuntimeError for human-readable failures
        # (repo not found, branch can't be created, empty repo).
        audit.log(actor_id, "http_push_failed", extra={"repo": body.repo, "branch": body.branch, "error": str(e)})
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001 — we want the audit log + 500
        audit.log(actor_id, "http_push_failed", extra={"repo": body.repo, "branch": body.branch, "error": str(e)})
        raise HTTPException(status_code=500, detail=f"push failed: {e}")

    audit.log(
        actor_id,
        "http_push",
        commit_url=url,
        extra={"repo": body.repo, "branch": body.branch or "(default)", "files": len(body.files)},
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
