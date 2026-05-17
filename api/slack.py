"""Slack `/tfgen` slash-command handler.

Registers a single endpoint on the shared `app` from `api.index`:

  POST /api/slack/tfgen   Slack signing-secret verified, slash-command body

Flow:

  1. Verify the request signature with `SLACK_SIGNING_SECRET` against the
     raw body and timestamp. Reject replays older than 5 minutes.
  2. Parse the form fields. Empty `text` returns an immediate ephemeral
     usage hint, no generation triggered.
  3. Spawn an asyncio background task and return an immediate ephemeral
     "Working on it" reply within Slack's 3-second slash-command deadline.
  4. The background task runs the same redact -> quota -> generate ->
     build files -> push pipeline that the HTTP handler uses, then POSTs
     the result (commit URL + a fenced okta.tf preview) to `response_url`.
     Errors and quota-exhaustion are surfaced to the user via the same
     `response_url`.

Reused from the HTTP surface:

  * `_bootstrap.prepare_client` and `_bootstrap.quota_blocked` for the
    cost-wrapped Anthropic client + daily-quota check.
  * `api.index._build_files` for the outputs -> filename map.
  * `audit.log`, `cost.today_usd`, `redact.redact` directly.

Background-task pattern: FastAPI's `BackgroundTasks` dependency. We
considered `asyncio.create_task` but it caused TestClient to hang on
portal teardown under Python 3.14 + anyio (the unfinished coroutine
keeps the portal's event loop alive past the request boundary).
`BackgroundTasks` runs after the response is sent but before the
portal exits, which Starlette's TestClient handles cleanly. Both are
valid on Vercel Fluid Compute (no 3s wall-clock cap on the worker).

Branch naming for Slack-originated pushes is fixed at `tfgen-slack`. A
per-request branch (e.g. `slack/<request_id>`) was considered but each
generation creating a new branch would clutter the target repo; the user
can always rename the branch after merge.
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from typing import Any

import requests
from fastapi import BackgroundTasks, HTTPException, Request

import audit
import cost
import redact
from core import service as core_service
from gh_push.push import push_to_github
from headless_rate_limit import (
    INPUT_LENGTH_CAP_BYTES,
    SLACK_RATE_LIMITER,
    check_input_length,
)

from api._bootstrap import prepare_client, quota_blocked
from api.index import app, _build_files


# ─── constants ───────────────────────────────────────────────────────────

# Slack rejects requests older than 5 minutes; we mirror that on the
# server side as replay protection. The signing-secret check alone is
# not enough since a captured signed request stays valid forever
# without a freshness window.
_SLACK_TIMESTAMP_MAX_AGE_SEC = 60 * 5

# Slack message text limit is 40,000 chars but a code block at the top of
# a response gets unwieldy fast. 1200 chars is enough to show one
# moderate okta.tf resource block + header without scrolling.
_PREVIEW_MAX_CHARS = 1200

# Branch all Slack pushes land on. Keeping a single shared branch means
# successive Slack generations stack on top of each other instead of
# fanning out into hundreds of single-commit branches.
_SLACK_BRANCH = "tfgen-slack"

# Output mode is fixed for Slack v1: the slash command takes free-form
# text, not a structured form. "Both" matches the Streamlit default.
_SLACK_OUTPUT_MODE = "Both"


# ─── signature verification ──────────────────────────────────────────────


def verify_slack_signature(body: bytes, timestamp: str, signature: str) -> None:
    """Validate a Slack request signature. Raises HTTPException on
    failure; returns None on success.

    Stale-timestamp check is enforced *before* the cryptographic check
    so we don't waste an HMAC round on obvious replays. Both checks are
    required: a fresh timestamp with a wrong signature is forgery, and
    a valid signature on an old timestamp is a replay.

    Raises 503 if the server has no `SLACK_SIGNING_SECRET` configured.
    """
    secret = (os.environ.get("SLACK_SIGNING_SECRET") or "").strip()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="server is not configured: SLACK_SIGNING_SECRET missing",
        )

    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="missing slack signature headers")

    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="invalid slack timestamp")

    if abs(time.time() - ts_int) > _SLACK_TIMESTAMP_MAX_AGE_SEC:
        raise HTTPException(status_code=401, detail="stale slack timestamp")

    # Local import so the module stays import-safe in environments
    # where slack_sdk is unavailable, even though it's in requirements.
    from slack_sdk.signature import SignatureVerifier

    verifier = SignatureVerifier(signing_secret=secret)
    if not verifier.is_valid(body=body, timestamp=timestamp, signature=signature):
        raise HTTPException(status_code=401, detail="invalid slack signature")


# ─── helpers ─────────────────────────────────────────────────────────────


def _actor_id_for_slack_user(slack_user_id: str) -> str:
    """16-char SHA256 prefix of the Slack user id. Mirrors
    `_auth.actor_id_for` so audit + cost end up keyed the same way as
    HTTP callers (different hash inputs, same shape).
    """
    return hashlib.sha256((slack_user_id or "").encode("utf-8")).hexdigest()[:16]


def _post_response(response_url: str, text: str, *, in_channel: bool = False) -> None:
    """POST a follow-up message to Slack's `response_url`. Best effort;
    logs and swallows on failure so an unreachable Slack endpoint can't
    take down the background task with an unhandled exception (the
    user-facing message is gone, but the audit log + cost meter still
    record what happened).
    """
    payload = {
        "response_type": "in_channel" if in_channel else "ephemeral",
        "text": text,
    }
    try:
        requests.post(response_url, json=payload, timeout=10)
    except Exception:
        # We can't reach Slack; nothing useful to do here. The audit log
        # is the source of truth.
        pass


def _truncate_preview(content: str, limit: int = _PREVIEW_MAX_CHARS) -> str:
    """Trim a long file to fit inside a Slack code block. Adds a
    `... (truncated)` marker when it actually clips."""
    if len(content) <= limit:
        return content
    return content[:limit].rstrip() + "\n... (truncated)"


def _format_success_message(commit_url: str, files: dict[str, str]) -> str:
    """Build the in-channel success message: commit link + fenced
    preview of the most interesting file (terraform/okta.tf if present,
    otherwise the first file we find). Falls back to a bare list when
    no files were generated.
    """
    if not files:
        return f"Generation complete. No files produced.\n{commit_url}"

    preview_path = "terraform/okta.tf" if "terraform/okta.tf" in files else next(iter(files))
    preview_body = _truncate_preview(files[preview_path])
    file_list = ", ".join(sorted(files.keys()))
    return (
        f"Pushed to {commit_url}\n"
        f"Files: {file_list}\n"
        f"```{preview_path}\n{preview_body}\n```"
    )


def _format_files_only_message(files: dict[str, str], reason: str) -> str:
    """Fallback message for the case where generation succeeded but the
    push step failed. We still return useful content to the user as a
    fenced block."""
    if not files:
        return f"Generation succeeded but push failed: {reason}. No files to display."
    preview_path = "terraform/okta.tf" if "terraform/okta.tf" in files else next(iter(files))
    preview_body = _truncate_preview(files[preview_path])
    file_list = ", ".join(sorted(files.keys()))
    return (
        f"Generation succeeded but push failed: {reason}\n"
        f"Files: {file_list}\n"
        f"```{preview_path}\n{preview_body}\n```"
    )


# ─── background pipeline ─────────────────────────────────────────────────


def _run_generation(
    *,
    prompt: str,
    slack_user_id: str,
    channel_id: str,
    response_url: str,
    request_id: str,
) -> None:
    """Background task for a single `/tfgen` invocation.

    Sync signature so FastAPI's BackgroundTasks runs it on a worker
    thread (it would block the event loop if it were async, since
    `core.service.generate` does synchronous network I/O internally).
    The goal is to release the slash-command HTTP request inside
    Slack's 3s deadline, not to maximize concurrency on a single
    worker.
    """
    actor_id = _actor_id_for_slack_user(slack_user_id)

    cleaned_prompt, redact_summary = redact.redact(prompt)

    # Build the cost-wrapped client and check the daily quota.
    try:
        ctx = prepare_client(actor_id)
    except RuntimeError as e:
        _post_response(response_url, f"Configuration error: {e}")
        audit.log(
            actor_id, "slack_generate_failed",
            extra={
                "surface": "slack",
                "slack_user_id": slack_user_id,
                "channel_id": channel_id,
                "error": str(e),
                "request_id": request_id,
            },
        )
        return

    blocked, spent, quota = quota_blocked(ctx)
    if blocked:
        _post_response(
            response_url,
            f"Daily quota exhausted: ${spent:.2f} / ${quota:.2f} USD. Try again tomorrow.",
        )
        audit.log(
            actor_id, "quota_blocked",
            extra={
                "surface": "slack",
                "slack_user_id": slack_user_id,
                "channel_id": channel_id,
                "spent_usd": spent,
                "quota_usd": quota,
                "request_id": request_id,
            },
        )
        return

    # Run the generate -> refine -> sanitize pipeline. core.service
    # already swallows pass-level errors and surfaces them as
    # GenerateResult.error, so a bare except here would only catch
    # truly unexpected failures (network blip, OOM).
    try:
        result = core_service.generate(
            cleaned_prompt,
            client=ctx.client,
            model=ctx.model,
            output_mode=_SLACK_OUTPUT_MODE,
        )
    except Exception as e:  # noqa: BLE001
        _post_response(response_url, f"Generation crashed: {e}")
        audit.log(
            actor_id, "slack_generate_failed",
            cost_estimate_usd=max(0.0, cost.today_usd(actor_id) - spent),
            redacted_input_preview=cleaned_prompt[:200],
            extra={
                "surface": "slack",
                "slack_user_id": slack_user_id,
                "channel_id": channel_id,
                "redact_summary": redact_summary,
                "error": str(e),
                "request_id": request_id,
            },
        )
        return

    today_after = cost.today_usd(actor_id)
    cost_delta = max(0.0, today_after - spent)

    if result.error:
        _post_response(response_url, f"Generation failed: {result.error}")
        audit.log(
            actor_id, "slack_generate",
            resource_type=(result.intent or {}).get("resource_type", ""),
            output_mode=_SLACK_OUTPUT_MODE,
            cost_estimate_usd=cost_delta,
            redacted_input_preview=cleaned_prompt[:200],
            extra={
                "surface": "slack",
                "slack_user_id": slack_user_id,
                "channel_id": channel_id,
                "redact_summary": redact_summary,
                "error": result.error,
                "request_id": request_id,
            },
        )
        return

    files = _build_files(result.outputs or {}, _SLACK_OUTPUT_MODE)

    # Push the file map to the shared Slack repo. SLACK_DEFAULT_REPO is
    # checked synchronously in the request handler, but the env can in
    # principle change between the immediate response and the
    # background run; be defensive.
    repo = (os.environ.get("SLACK_DEFAULT_REPO") or "").strip()
    gh_token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if not repo or not gh_token:
        _post_response(
            response_url,
            _format_files_only_message(files, "SLACK_DEFAULT_REPO or GITHUB_TOKEN missing"),
        )
        audit.log(
            actor_id, "slack_generate",
            resource_type=(result.intent or {}).get("resource_type", ""),
            output_mode=_SLACK_OUTPUT_MODE,
            cost_estimate_usd=cost_delta,
            redacted_input_preview=cleaned_prompt[:200],
            extra={
                "surface": "slack",
                "slack_user_id": slack_user_id,
                "channel_id": channel_id,
                "redact_summary": redact_summary,
                "push_skipped": True,
                "request_id": request_id,
            },
        )
        return

    commit_url = ""
    push_error = ""
    try:
        commit_url = push_to_github(
            files,
            repo,
            gh_token,
            f"feat(slack): {cleaned_prompt[:60]}",
            branch=_SLACK_BRANCH,
        )
    except RuntimeError as e:
        push_error = str(e)
    except Exception as e:  # noqa: BLE001
        push_error = f"unexpected push error: {e}"

    if commit_url:
        _post_response(
            response_url,
            _format_success_message(commit_url, files),
            in_channel=True,
        )
    else:
        _post_response(response_url, _format_files_only_message(files, push_error or "unknown"))

    audit.log(
        actor_id, "slack_generate",
        resource_type=(result.intent or {}).get("resource_type", ""),
        output_mode=_SLACK_OUTPUT_MODE,
        cost_estimate_usd=cost_delta,
        commit_url=commit_url,
        redacted_input_preview=cleaned_prompt[:200],
        extra={
            "surface": "slack",
            "slack_user_id": slack_user_id,
            "channel_id": channel_id,
            "redact_summary": redact_summary,
            "push_error": push_error,
            "request_id": request_id,
        },
    )


# ─── route ───────────────────────────────────────────────────────────────


@app.post("/api/slack/tfgen")
async def slack_tfgen(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Slash-command entry point. Returns within Slack's 3s deadline.

    Synchronous response body uses `response_type: ephemeral` so only
    the invoking user sees the "Working on it" placeholder. The
    background task switches to `in_channel` on the success path so the
    final result is visible to the whole channel.
    """
    body = await request.body()

    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    # Verify before parsing so a forged request never reaches our
    # form-decode logic.
    verify_slack_signature(body, timestamp, signature)

    form = await request.form()
    text = (form.get("text") or "").strip()
    slack_user_id = (form.get("user_id") or "").strip()
    channel_id = (form.get("channel_id") or "").strip()
    response_url = (form.get("response_url") or "").strip()

    if not text:
        return {
            "response_type": "ephemeral",
            "text": (
                "Usage: /tfgen <plain-English description>\n"
                "Example: /tfgen create an Engineering Okta group"
            ),
        }

    # Rate limit + input-length checks run synchronously inside the
    # slash-command request so a flood of /tfgen calls from one user
    # never queues background tasks. Ephemeral text + HTTP 200 is the
    # Slack convention for "show the user a message" without surfacing
    # an error to the Slack workspace.
    rl_actor = _actor_id_for_slack_user(slack_user_id)
    allowed, retry_after = SLACK_RATE_LIMITER.check(slack_user_id or "anonymous")
    if not allowed:
        audit.log(
            rl_actor,
            "rate_limited_slack",
            extra={
                "surface": "slack",
                "slack_user_id": slack_user_id,
                "channel_id": channel_id,
                "retry_after_seconds": retry_after,
            },
        )
        return {
            "response_type": "ephemeral",
            "text": f"You're being rate limited. Try again in {retry_after} seconds.",
        }

    ok, reason = check_input_length(text, INPUT_LENGTH_CAP_BYTES)
    if not ok:
        audit.log(
            rl_actor,
            "input_too_large_slack",
            extra={
                "surface": "slack",
                "slack_user_id": slack_user_id,
                "channel_id": channel_id,
                "reason": reason,
                "cap_bytes": INPUT_LENGTH_CAP_BYTES,
            },
        )
        return {
            "response_type": "ephemeral",
            "text": (
                f"Your prompt is too long ({reason}). "
                f"Shorten it to fit within {INPUT_LENGTH_CAP_BYTES} bytes and try again."
            ),
        }

    if not response_url:
        # Without a response_url we can't deliver the result.
        # Surface the failure synchronously rather than spawn a task
        # that has nowhere to write back to.
        return {
            "response_type": "ephemeral",
            "text": "Slack did not provide a response_url for this command. Cannot proceed.",
        }

    repo = (os.environ.get("SLACK_DEFAULT_REPO") or "").strip()
    if not repo:
        return {
            "response_type": "ephemeral",
            "text": (
                "Server is not configured: SLACK_DEFAULT_REPO is unset. "
                "Ask the admin to set the target repo for Slack-originated pushes."
            ),
        }

    request_id = uuid.uuid4().hex
    background_tasks.add_task(
        _run_generation,
        prompt=text,
        slack_user_id=slack_user_id,
        channel_id=channel_id,
        response_url=response_url,
        request_id=request_id,
    )

    return {
        "response_type": "ephemeral",
        "text": "Working on it... I'll post the result here when generation finishes (~30s).",
    }
