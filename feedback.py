"""User feedback → GitHub issue bridge for the TF Tool.

Posts a structured GitHub issue every time a user submits feedback on a
generated output. Issue lands in the configured `FEEDBACK_REPO` secret
(falls back to `GITHUB_REPO` so the same repo as audit / cost is used
when feedback isn't routed elsewhere).

Module load is side-effect free. Configure once at app startup with
`feedback.configure(github_token, feedback_repo)` then call
`feedback.submit(...)` from the widget submit handler.

Failures never raise — a bad feedback POST cannot break the user's
workflow. submit() returns the issue URL on success or None on failure.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

_github_token: str = ""
_feedback_repo: str = ""


def configure(github_token: str, feedback_repo: str) -> None:
    global _github_token, _feedback_repo
    _github_token = (github_token or "").strip()
    _feedback_repo = (feedback_repo or "").strip()


def is_configured() -> bool:
    return bool(_github_token and _feedback_repo)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def submit(
    *,
    email: str,
    sentiment: str,
    comment: str,
    intent: dict | None,
    user_input: str,
    output_summary: dict | None = None,
) -> str | None:
    """Post a feedback issue. sentiment must be 'up' or 'down'. Returns
    the issue URL on success, None on any failure."""
    if not is_configured():
        return None
    if sentiment not in ("up", "down"):
        return None

    title = _build_title(intent, sentiment)
    body = _build_body(
        email=email,
        sentiment=sentiment,
        comment=comment,
        intent=intent or {},
        user_input=user_input,
        output_summary=output_summary or {},
    )
    labels = ["feedback", f"sentiment-{'positive' if sentiment == 'up' else 'negative'}"]

    try:
        from github import Github
        g = Github(_github_token)
        repo = g.get_repo(_feedback_repo)
        issue = repo.create_issue(title=title, body=body, labels=labels)
        return issue.html_url
    except Exception:
        return None


def _build_title(intent: dict | None, sentiment: str) -> str:
    op = (intent or {}).get("operation_type", "operation")
    rt = (intent or {}).get("resource_type", "resource")
    icon = "👍" if sentiment == "up" else "👎"
    return f"{icon} Feedback: {op} {rt}"


def _build_body(
    *,
    email: str,
    sentiment: str,
    comment: str,
    intent: dict,
    user_input: str,
    output_summary: dict,
) -> str:
    sentiment_label = "Positive" if sentiment == "up" else "Negative"
    comment_block = comment.strip() or "_(no free-text comment)_"
    intent_pretty = json.dumps(intent, indent=2, default=str)
    summary_pretty = json.dumps(output_summary, indent=2, default=str) if output_summary else "_(no summary)_"
    return (
        f"## {sentiment_label} feedback\n\n"
        f"**Submitted by:** `{email}`  \n"
        f"**Submitted at:** `{_now_iso()}`\n\n"
        f"### Comment\n\n{comment_block}\n\n"
        f"### User prompt\n\n```\n{user_input.strip()}\n```\n\n"
        f"### Parsed intent\n\n```json\n{intent_pretty}\n```\n\n"
        f"### Output summary\n\n```json\n{summary_pretty}\n```\n"
    )
