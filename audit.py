"""Append-only audit log for privileged actions in the TF Tool.

Every action that touches secrets, generates code, pushes to GitHub, or
mutates state lands here. Customers' IT teams can pull these logs to
prove what was done with the secrets they granted us access to.

Storage shape mirrors history.py: per-user JSONL file in the configured
GitHub repo at `_tftool/audit/<email-hash>.jsonl`. Append-only. Never
edit, never delete. The 16-char SHA256 prefix of the email keeps the
filename stable but does not contain PII.

When GitHub is not configured, falls back to a local file under
`.streamlit/audit_local.jsonl` so dev environments still produce a log.

This module is intentionally side-effect free at import time (no
streamlit, no GitHub, no env reads). Configure once at app startup via
`audit.configure(github_token, github_repo)` then call `audit.log(...)`
from action sites.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import uuid
from datetime import datetime, timezone

_GH_DIR = "_tftool/audit"
_LOCAL_PATH = ".streamlit/audit_local.jsonl"

# Bound the sidebar fetch + CSV-export. Full history stays in GitHub.
MAX_RECENT_ENTRIES = 200

_github_token: str = ""
_github_repo: str = ""


def configure(github_token: str, github_repo: str) -> None:
    """Configure GitHub destination once at app startup."""
    global _github_token, _github_repo
    _github_token = (github_token or "").strip()
    _github_repo = (github_repo or "").strip()


def _email_hash(email: str) -> str:
    return hashlib.sha256((email or "anonymous").encode("utf-8")).hexdigest()[:16]


def _gh_path(email: str) -> str:
    return f"{_GH_DIR}/{_email_hash(email)}.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(
    email: str,
    action: str,
    *,
    resource_type: str = "",
    output_mode: str = "",
    cost_estimate_usd: float = 0.0,
    commit_url: str = "",
    redacted_input_preview: str = "",
    extra: dict | None = None,
) -> dict:
    """Append a single audit record. Returns the record dict so callers can
    correlate via request_id. Never raises; logging failures are swallowed
    by design so an audit-write outage cannot break the user's workflow.
    """
    record = {
        "timestamp_utc": _now_iso(),
        "actor_email": email or "anonymous",
        "action": action,
        "resource_type": resource_type,
        "output_mode": output_mode,
        "cost_estimate_usd": round(float(cost_estimate_usd or 0.0), 6),
        "commit_url": commit_url,
        "redacted_input_preview": (redacted_input_preview or "")[:200],
        "request_id": uuid.uuid4().hex,
    }
    if extra:
        record["extra"] = extra
    line = json.dumps(record, separators=(",", ":")) + "\n"

    if _github_token and _github_repo:
        try:
            _append_to_github(email, line)
            return record
        except Exception:
            # fall through to local on any GitHub error
            pass
    _append_local(line)
    return record


def _append_to_github(email: str, line: str) -> None:
    # Local import keeps module import-side-effect free.
    from github import Github, GithubException

    g = Github(_github_token)
    repo = g.get_repo(_github_repo)
    path = _gh_path(email)
    msg = "chore(audit): append entry"
    try:
        existing = repo.get_contents(path)
        prev = base64.b64decode(existing.content).decode("utf-8")
        repo.update_file(path, msg, prev + line, existing.sha)
    except GithubException as e:
        if e.status == 404:
            repo.create_file(path, msg, line)
        else:
            raise


def _append_local(line: str) -> None:
    try:
        os.makedirs(os.path.dirname(_LOCAL_PATH), exist_ok=True)
        with open(_LOCAL_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def recent(email: str, limit: int = 10) -> list[dict]:
    """Return the most recent audit entries for the given user (newest first).
    Falls back to the local log when GitHub isn't configured. Returns at
    most `limit` entries; total fetched is capped at MAX_RECENT_ENTRIES.
    """
    raw = _load_text(email)
    if not raw:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    out.reverse()  # newest first
    return out[: max(0, limit)]


def export_csv(email: str) -> str:
    """Return the user's full audit log as CSV text."""
    raw = _load_text(email)
    rows: list[dict] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not rows:
        return ""
    fieldnames = sorted({k for r in rows for k in r.keys()})
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
    return buf.getvalue()


def _load_text(email: str) -> str:
    if _github_token and _github_repo:
        try:
            from github import Github, GithubException
            g = Github(_github_token)
            repo = g.get_repo(_github_repo)
            try:
                contents = repo.get_contents(_gh_path(email))
                return base64.b64decode(contents.content).decode("utf-8")
            except GithubException as e:
                if e.status == 404:
                    return ""
                raise
        except Exception:
            return ""
    try:
        with open(_LOCAL_PATH, encoding="utf-8") as f:
            return f.read()
    except (FileNotFoundError, OSError):
        return ""
