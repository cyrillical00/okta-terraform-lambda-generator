"""Per-user key-value preferences for the TF Tool.

Stores small, non-sensitive UI preferences (onboarding-seen flag, theme
choice, etc.) per signed-in user. Persisted as JSON in the configured
GitHub repo at `_tftool/user_prefs/<email-hash>.json`, with a local
fallback under `.streamlit/user_prefs_local.json` (a flat dict keyed by
email-hash) when GitHub is not configured.

Mirrors the audit.py module shape: import is side-effect free, callers
configure once at startup via `user_prefs.configure(...)`, then
`user_prefs.load(email)` / `user_prefs.save(email, prefs)`.

Failures never raise — UI prefs are best-effort and a write failure
(e.g. transient GitHub outage) must not block the user's workflow.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

_GH_DIR = "_tftool/user_prefs"
_LOCAL_PATH = ".streamlit/user_prefs_local.json"

_github_token: str = ""
_github_repo: str = ""


def configure(github_token: str, github_repo: str) -> None:
    global _github_token, _github_repo
    _github_token = (github_token or "").strip()
    _github_repo = (github_repo or "").strip()


def _email_hash(email: str) -> str:
    return hashlib.sha256((email or "anonymous").encode("utf-8")).hexdigest()[:16]


def _gh_path(email: str) -> str:
    return f"{_GH_DIR}/{_email_hash(email)}.json"


def load(email: str) -> dict[str, Any]:
    """Return the saved preferences for the given user. Empty dict on miss
    or any error so callers can use `.get(...)` directly."""
    if _github_token and _github_repo:
        try:
            from github import Github, GithubException
            g = Github(_github_token)
            repo = g.get_repo(_github_repo)
            try:
                contents = repo.get_contents(_gh_path(email))
                raw = base64.b64decode(contents.content).decode("utf-8")
                return json.loads(raw) if raw else {}
            except GithubException as e:
                if e.status == 404:
                    return {}
                raise
        except Exception:
            pass
    return _load_local(email)


def save(email: str, prefs: dict[str, Any]) -> None:
    """Persist the full prefs dict for the user. Replaces any prior value.
    Best-effort; failures are swallowed."""
    payload = json.dumps(prefs, separators=(",", ":"))
    if _github_token and _github_repo:
        try:
            _save_to_github(email, payload)
            return
        except Exception:
            pass
    _save_local(email, prefs)


def update(email: str, **kv: Any) -> dict[str, Any]:
    """Merge the given kwargs into the user's existing prefs and persist.
    Returns the merged dict."""
    current = load(email)
    current.update(kv)
    save(email, current)
    return current


def _save_to_github(email: str, payload: str) -> None:
    from github import Github, GithubException
    g = Github(_github_token)
    repo = g.get_repo(_github_repo)
    path = _gh_path(email)
    msg = "chore(prefs): update user prefs"
    try:
        existing = repo.get_contents(path)
        repo.update_file(path, msg, payload, existing.sha)
    except GithubException as e:
        if e.status == 404:
            repo.create_file(path, msg, payload)
        else:
            raise


def _load_local(email: str) -> dict[str, Any]:
    try:
        with open(_LOCAL_PATH, encoding="utf-8") as f:
            blob = json.load(f) or {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return blob.get(_email_hash(email), {}) if isinstance(blob, dict) else {}


def _save_local(email: str, prefs: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(_LOCAL_PATH), exist_ok=True)
        try:
            with open(_LOCAL_PATH, encoding="utf-8") as f:
                blob = json.load(f) or {}
            if not isinstance(blob, dict):
                blob = {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            blob = {}
        blob[_email_hash(email)] = prefs
        with open(_LOCAL_PATH, "w", encoding="utf-8") as f:
            json.dump(blob, f, separators=(",", ":"))
    except OSError:
        pass
