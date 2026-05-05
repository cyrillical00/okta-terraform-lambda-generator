"""JIRA webhook handler for the TF Tool.

Registers a single endpoint on the shared FastAPI `app` from
`api.index`:

  POST /api/jira/webhook   HMAC-SHA256 signature required

Flow:

JIRA Cloud (or a JIRA Automation rule / front proxy) POSTs an issue
event payload here. We:

  1. Verify the HMAC-SHA256 signature in the `X-Hub-Signature` header
     against `JIRA_WEBHOOK_SECRET`. Constant-time compare; reject 401
     on mismatch.
  2. Parse the payload; bail with a 200 no-op for events we don't
     care about (delete, worklog, etc.) and for issues that lack the
     `tfgen` label. JIRA fires webhooks on every event, so filter
     early to keep load down.
  3. Build the prompt from `summary` + `description` (the description
     can be plaintext or Atlassian Document Format JSON; we walk ADF
     for `text` nodes when needed).
  4. Run the canonical generate flow: redact, quota check, cost-wrapped
     Anthropic client, `core.service.generate`, push to
     `JIRA_DEFAULT_REPO` on branch `jira/<issue_key>`.
  5. Post a JIRA comment with the commit URL and a fenced preview of
     `terraform/okta.tf`. Optionally transition the issue (off by
     default; project-specific transition IDs).

JIRA's webhook delivery is fire-and-forget, with a typical 30-second
window for a 2xx response. Vercel Fluid Compute (800s maxDuration)
handles a synchronous generate inside that window, so this handler
returns synchronously after generation completes; no background task
gymnastics.

Auth model:

JIRA Cloud's signed-webhook story varies by install: some setups use
Atlassian Connect JWT, some use shared secrets validated differently.
For v1, this handler expects an HMAC-SHA256 signature in
`X-Hub-Signature: sha256=<hex>` form, which is what JIRA Automation
rules and most reverse-proxy setups produce. If a future install
uses Connect JWT instead, swap the verifier; the rest of the flow
stays the same.

Environment:

Required:
  JIRA_WEBHOOK_SECRET   shared secret for HMAC verification
  JIRA_DEFAULT_REPO     owner/repo for pushes
  JIRA_USER_EMAIL       JIRA Cloud account email (for callback Basic auth)
  JIRA_API_TOKEN        JIRA Cloud API token (paired with the email)
  GITHUB_TOKEN          for the GitHub push (also used by audit/cost)
  ANTHROPIC_API_KEY     for the LLM client

Optional:
  JIRA_AUTO_TRANSITION  "1" to auto-transition issues after pushing
  JIRA_TRANSITION_ID    transition ID to use (project-specific)
  ANTHROPIC_MODEL       defaults to claude-haiku-4-5-20251001
"""

from __future__ import annotations

import hashlib
import hmac
import json as _json
import os
from typing import Any
from urllib.parse import urlparse

import requests
from fastapi import HTTPException, Request

import audit
import cost
import redact
from core import service as core_service
from gh_push.push import push_to_github

from api.index import _build_files, app
from api._bootstrap import prepare_client, quota_blocked


# ─── HMAC verification ───────────────────────────────────────────────────


def _expected_secret() -> str:
    return (os.environ.get("JIRA_WEBHOOK_SECRET") or "").strip()


def verify_jira_signature(body: bytes, header_value: str | None) -> None:
    """Validate `X-Hub-Signature: sha256=<hex>` against the raw body.

    Raises HTTPException(401) on missing or mismatched signature, and
    HTTPException(503) when the server has no secret configured (so a
    caller doesn't think their signature is wrong when in fact the
    server has no way to verify anything).
    """
    secret = _expected_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="server is not configured: JIRA_WEBHOOK_SECRET missing")

    provided = (header_value or "").strip()
    if not provided:
        raise HTTPException(status_code=401, detail="missing X-Hub-Signature header")

    # Accept both "sha256=<hex>" and bare "<hex>" forms; strip the prefix
    # if present so the compare lines up.
    if provided.lower().startswith("sha256="):
        provided_hex = provided.split("=", 1)[1].strip()
    else:
        provided_hex = provided

    expected_hex = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided_hex.lower(), expected_hex.lower()):
        raise HTTPException(status_code=401, detail="invalid X-Hub-Signature")


# ─── ADF parsing ─────────────────────────────────────────────────────────


_BLOCK_TYPES = {"paragraph", "heading", "blockquote", "listItem", "codeBlock"}


def _adf_to_text(adf: Any) -> str:
    """Walk an Atlassian Document Format value and concatenate `text`
    fields into a plaintext string. Newer JIRA Cloud APIs return ADF
    JSON for `description`; older or proxied installs send a string.
    Defensive: returns "" for anything it doesn't understand.

    Inline text nodes inside a paragraph are joined without a separator
    so consecutive `{"type": "text", "text": "..."}` siblings produce
    the original sentence. Block-level nodes (paragraph, heading, etc.)
    are separated by blank lines so multi-paragraph descriptions read
    correctly.
    """
    if adf is None:
        return ""
    if isinstance(adf, str):
        return adf
    if isinstance(adf, list):
        # A list of mixed inline + block children: join blocks with a
        # blank line, inline text with no separator. We detect blocks by
        # looking at child dicts' `type`.
        rendered: list[str] = []
        for x in adf:
            if x is None:
                continue
            r = _adf_to_text(x)
            if not r:
                continue
            is_block = isinstance(x, dict) and x.get("type") in _BLOCK_TYPES
            rendered.append(("\n\n" + r) if is_block and rendered else r)
        return "".join(rendered).strip()
    if isinstance(adf, dict):
        node_type = adf.get("type")
        # Direct text leaf.
        if node_type == "text" and isinstance(adf.get("text"), str):
            return adf["text"]
        # Recurse into children.
        inner = _adf_to_text(adf.get("content")) if "content" in adf else ""
        if node_type in _BLOCK_TYPES:
            return inner.strip()
        return inner
    return ""


# ─── JIRA REST callbacks ─────────────────────────────────────────────────


def _jira_base_url(issue_self: str) -> str:
    """Extract the JIRA base URL from an issue's `self` link.

    e.g. https://company.atlassian.net/rest/api/3/issue/PROJ-123
         -> https://company.atlassian.net
    Falls back to "" so a missing `self` doesn't crash the handler;
    the caller checks for empty before issuing callback requests.
    """
    if not issue_self:
        return ""
    parsed = urlparse(issue_self)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _jira_auth() -> tuple[str, str] | None:
    email = (os.environ.get("JIRA_USER_EMAIL") or "").strip()
    token = (os.environ.get("JIRA_API_TOKEN") or "").strip()
    if not email or not token:
        return None
    return (email, token)


def _post_comment(base_url: str, issue_key: str, body_text: str) -> tuple[bool, str]:
    """Post a plaintext comment on the issue. Returns (ok, error_msg).

    Never raises; callers want to log the failure and continue rather
    than abort on a callback error.
    """
    if not base_url or not issue_key:
        return False, "missing base_url or issue_key"
    auth = _jira_auth()
    if auth is None:
        return False, "JIRA_USER_EMAIL or JIRA_API_TOKEN missing"
    url = f"{base_url}/rest/api/3/issue/{issue_key}/comment"
    payload = {"body": body_text}
    try:
        resp = requests.post(url, json=payload, auth=auth, timeout=15)
    except Exception as e:  # noqa: BLE001
        return False, f"request error: {e}"
    if 200 <= resp.status_code < 300:
        return True, ""
    return False, f"jira responded {resp.status_code}: {resp.text[:200]}"


def _transition_issue(base_url: str, issue_key: str, transition_id: str) -> tuple[bool, str]:
    if not base_url or not issue_key or not transition_id:
        return False, "missing base_url, issue_key, or transition_id"
    auth = _jira_auth()
    if auth is None:
        return False, "JIRA_USER_EMAIL or JIRA_API_TOKEN missing"
    url = f"{base_url}/rest/api/3/issue/{issue_key}/transitions"
    payload = {"transition": {"id": transition_id}}
    try:
        resp = requests.post(url, json=payload, auth=auth, timeout=15)
    except Exception as e:  # noqa: BLE001
        return False, f"request error: {e}"
    if 200 <= resp.status_code < 300:
        return True, ""
    return False, f"jira responded {resp.status_code}: {resp.text[:200]}"


# ─── route ───────────────────────────────────────────────────────────────


_PROCESSED_EVENTS = {"jira:issue_created", "jira:issue_updated"}
_LABEL_FILTER = "tfgen"
_PREVIEW_MAX = 2000


@app.post("/api/jira/webhook")
async def jira_webhook(request: Request) -> dict:
    """Process a JIRA Cloud webhook event.

    Returns 200 with a status payload on every accepted event (including
    no-ops for events we ignore). 401 on bad signature, 503 when the
    server isn't configured. Generation errors and push errors are
    surfaced as JIRA comments and still return 200, because JIRA will
    otherwise retry on non-2xx responses, which would compound a
    transient failure.
    """
    body = await request.body()
    sig_header = request.headers.get("x-hub-signature") or request.headers.get("X-Hub-Signature")
    verify_jira_signature(body, sig_header)

    try:
        payload = _json.loads(body.decode("utf-8") or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    event = (payload.get("webhookEvent") or "").strip()
    if event not in _PROCESSED_EVENTS:
        return {"status": "ignored", "reason": f"unhandled event: {event or '(empty)'}"}

    issue = payload.get("issue") or {}
    issue_key = (issue.get("key") or "").strip()
    fields = issue.get("fields") or {}
    labels = fields.get("labels") or []
    if _LABEL_FILTER not in labels:
        return {"status": "ignored", "reason": "no tfgen label"}

    summary = (fields.get("summary") or "").strip()
    description_raw = fields.get("description")
    description_text = _adf_to_text(description_raw).strip()

    if not summary and not description_text:
        return {"status": "ignored", "reason": "empty summary and description"}

    prompt = summary if not description_text else f"{summary}\n\n{description_text}".strip()

    creator = (fields.get("creator") or {}) if isinstance(fields.get("creator"), dict) else {}
    creator_email = (creator.get("emailAddress") or "").strip()
    creator_account = (creator.get("accountId") or "").strip()
    actor_label = creator_email or creator_account or "jira-anonymous"
    actor_id = hashlib.sha256(actor_label.encode("utf-8")).hexdigest()[:16]

    project_key = ""
    if "-" in issue_key:
        project_key = issue_key.split("-", 1)[0]

    base_url = _jira_base_url(issue.get("self") or "")

    # ─── prepare client + quota check ────────────────────────────────────
    try:
        ctx = prepare_client(actor_id)
    except RuntimeError as e:
        # Server config error; surface the message but log via audit so
        # we can correlate.
        audit.log(
            actor_label, "jira_config_error",
            extra={"surface": "jira", "issue_key": issue_key, "error": str(e)},
        )
        raise HTTPException(status_code=503, detail=str(e))

    blocked, spent, quota = quota_blocked(ctx)
    if blocked:
        msg = f"TF Tool quota exceeded for today: ${spent:.2f} of ${quota:.2f} USD."
        ok, err = _post_comment(base_url, issue_key, msg)
        audit.log(
            actor_label, "quota_blocked",
            extra={
                "surface": "jira",
                "issue_key": issue_key,
                "project_key": project_key,
                "spent_usd": spent,
                "quota_usd": quota,
                "comment_ok": ok,
                "comment_error": err,
            },
        )
        return {"status": "quota_exceeded", "spent_usd": spent, "quota_usd": quota}

    # ─── redact + generate ───────────────────────────────────────────────
    cleaned_prompt, redact_summary = redact.redact(prompt)

    result = core_service.generate(
        cleaned_prompt,
        client=ctx.client,
        model=ctx.model,
        output_mode="Both",
    )

    today_after = cost.today_usd(actor_id)
    cost_delta = max(0.0, today_after - spent)

    if result.error or not result.outputs:
        err_text = result.error or "generation produced no outputs"
        comment = f"TF Tool could not generate Terraform for this issue.\n\nError: {err_text}"
        ok, err_post = _post_comment(base_url, issue_key, comment)
        audit.log(
            actor_label, "jira_generate",
            resource_type=(result.intent or {}).get("resource_type", ""),
            output_mode="Both",
            cost_estimate_usd=cost_delta,
            redacted_input_preview=cleaned_prompt[:200],
            extra={
                "surface": "jira",
                "issue_key": issue_key,
                "project_key": project_key,
                "redact_summary": redact_summary,
                "error": err_text,
                "comment_ok": ok,
                "comment_error": err_post,
            },
        )
        return {"status": "generation_failed", "error": err_text}

    files = _build_files(result.outputs, "Both")

    # ─── push ────────────────────────────────────────────────────────────
    repo = (os.environ.get("JIRA_DEFAULT_REPO") or "").strip()
    gh_token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    branch = f"jira/{issue_key}" if issue_key else ""
    commit_msg = f"feat: generate Terraform for {issue_key} via TF Tool"

    if not repo or not gh_token:
        missing = "JIRA_DEFAULT_REPO" if not repo else "GITHUB_TOKEN"
        msg = (
            f"TF Tool generated Terraform but cannot push: server is not configured ({missing} missing). "
            "Ask an admin to set the env var."
        )
        ok, err_post = _post_comment(base_url, issue_key, msg)
        audit.log(
            actor_label, "jira_push_skipped",
            resource_type=(result.intent or {}).get("resource_type", ""),
            output_mode="Both",
            cost_estimate_usd=cost_delta,
            redacted_input_preview=cleaned_prompt[:200],
            extra={
                "surface": "jira",
                "issue_key": issue_key,
                "project_key": project_key,
                "missing_env": missing,
                "comment_ok": ok,
                "comment_error": err_post,
            },
        )
        return {"status": "push_skipped", "missing": missing}

    commit_url = ""
    push_error = ""
    try:
        commit_url = push_to_github(files, repo, gh_token, commit_msg, branch=branch)
    except RuntimeError as e:
        push_error = str(e)
    except Exception as e:  # noqa: BLE001
        push_error = f"unexpected push error: {e}"

    if push_error:
        # Inline the raw files so the user can salvage the work even when
        # the push failed (repo missing, branch race, empty repo, etc.).
        inline_blocks: list[str] = []
        for path, content in files.items():
            snippet = content if len(content) <= _PREVIEW_MAX else content[:_PREVIEW_MAX] + "\n... (truncated)"
            inline_blocks.append(f"*{path}*\n```\n{snippet}\n```")
        comment = (
            f"TF Tool generated Terraform but the GitHub push failed.\n\n"
            f"Error: {push_error}\n\n"
            "Files (copy these manually):\n\n" + "\n\n".join(inline_blocks)
        )
        ok, err_post = _post_comment(base_url, issue_key, comment)
        audit.log(
            actor_label, "jira_push_failed",
            resource_type=(result.intent or {}).get("resource_type", ""),
            output_mode="Both",
            cost_estimate_usd=cost_delta,
            redacted_input_preview=cleaned_prompt[:200],
            extra={
                "surface": "jira",
                "issue_key": issue_key,
                "project_key": project_key,
                "repo": repo,
                "branch": branch,
                "push_error": push_error,
                "comment_ok": ok,
                "comment_error": err_post,
            },
        )
        return {"status": "push_failed", "error": push_error}

    # ─── success: comment with commit + preview ──────────────────────────
    okta_tf = files.get("terraform/okta.tf", "")
    preview = okta_tf if len(okta_tf) <= _PREVIEW_MAX else okta_tf[:_PREVIEW_MAX] + "\n... (truncated)"
    if preview:
        comment_body = (
            f"TF Tool generated Terraform and pushed to {commit_url}\n\n"
            f"Branch: `{branch}`\n\n"
            f"Preview of `terraform/okta.tf`:\n```\n{preview}\n```"
        )
    else:
        comment_body = (
            f"TF Tool generated Terraform and pushed to {commit_url}\n\n"
            f"Branch: `{branch}`"
        )
    comment_ok, comment_err = _post_comment(base_url, issue_key, comment_body)

    transitioned = False
    transition_err = ""
    if (os.environ.get("JIRA_AUTO_TRANSITION") or "").strip() == "1":
        transition_id = (os.environ.get("JIRA_TRANSITION_ID") or "").strip()
        if transition_id:
            transitioned, transition_err = _transition_issue(base_url, issue_key, transition_id)
        else:
            transition_err = "JIRA_TRANSITION_ID missing"

    audit.log(
        actor_label, "jira_generate",
        resource_type=(result.intent or {}).get("resource_type", ""),
        output_mode="Both",
        cost_estimate_usd=cost_delta,
        commit_url=commit_url,
        redacted_input_preview=cleaned_prompt[:200],
        extra={
            "surface": "jira",
            "issue_key": issue_key,
            "project_key": project_key,
            "repo": repo,
            "branch": branch,
            "files": list(files.keys()),
            "redact_summary": redact_summary,
            "comment_ok": comment_ok,
            "comment_error": comment_err,
            "transitioned": transitioned,
            "transition_error": transition_err,
        },
    )

    return {
        "status": "ok",
        "commit_url": commit_url,
        "issue_key": issue_key,
        "branch": branch,
        "files": list(files.keys()),
        "transitioned": transitioned,
    }
