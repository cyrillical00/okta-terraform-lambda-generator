"""Per-process initialization shared by every HTTP entry point.

Configures `audit` and `cost` against the same GitHub-backed storage
the Streamlit app uses, builds a usage-wrapped Anthropic client for the
caller, and exposes a single helper that produces everything an
endpoint needs to call `core.service.generate()`.

Vercel Python serverless reuses warm workers, so configuring
`audit`/`cost` once at module-import time is fine — `audit.configure`
is idempotent. Cold starts pay the import cost once.

Every HTTP / Slack / JIRA handler MUST go through `prepare_client`
rather than instantiating `anthropic.Anthropic` directly. That's how
the actor's daily spend ends up in `_tftool/usage/<hash>.json` and
their actions land in `_tftool/audit/<hash>.jsonl`. Skipping
`prepare_client` would make the new entry point invisible to the
audit pipeline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import anthropic

import audit
import cost


_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_DEFAULT_HTTP_QUOTA_USD = 5.00


@dataclass
class CallerContext:
    """Everything an HTTP handler needs to call core.service.generate.

    `actor_id` is the 16-char hash of the API key (or slack_user_id /
    jira_email for those entry points). Pass it as the `email` arg to
    audit.log() and cost.today_usd() — both modules hash internally,
    so the same actor_id maps to the same on-disk filename across
    every entry point.
    """
    actor_id: str
    client: anthropic.Anthropic  # cost-wrapped
    model: str
    daily_quota_usd: float


_configured = False


def _ensure_configured() -> None:
    """Idempotent. Reads env on first call; subsequent calls are no-ops.

    Reads `GITHUB_TOKEN` and `GITHUB_REPO` for audit/cost storage. When
    either is missing, `audit` and `cost` fall back to local files,
    which is fine for `vercel dev` but means production must have the
    env vars bound at the project level.
    """
    global _configured
    if _configured:
        return
    gh_token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    gh_repo = (os.environ.get("GITHUB_REPO") or "").strip()
    audit.configure(gh_token, gh_repo)
    cost.configure(gh_token, gh_repo)
    _configured = True


def prepare_client(actor_id: str) -> CallerContext:
    """Build a cost-wrapped Anthropic client + return the orchestration
    context for an HTTP / Slack / JIRA handler.

    Raises RuntimeError if ANTHROPIC_API_KEY is missing. The handler
    should let that propagate as a 500; Vercel logs surface the
    traceback.
    """
    _ensure_configured()
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("server is not configured: ANTHROPIC_API_KEY missing")

    raw = anthropic.Anthropic(api_key=api_key)
    wrapped = cost.wrap_client(raw, actor_id)

    return CallerContext(
        actor_id=actor_id,
        client=wrapped,
        model=(os.environ.get("ANTHROPIC_MODEL") or "").strip() or _DEFAULT_MODEL,
        daily_quota_usd=float(os.environ.get("TFGEN_HTTP_DAILY_QUOTA_USD", _DEFAULT_HTTP_QUOTA_USD)),
    )


def quota_blocked(ctx: CallerContext) -> tuple[bool, float, float]:
    """Check whether the caller has exceeded their daily quota.

    Returns (blocked, spent, quota). Single shared cap for v1 — all
    HTTP callers share TFGEN_HTTP_DAILY_QUOTA_USD. The `[api_keys]`
    table that would let each key carry its own quota is deferred to
    Phase 11+.
    """
    spent = cost.today_usd(ctx.actor_id)
    return spent >= ctx.daily_quota_usd, spent, ctx.daily_quota_usd
