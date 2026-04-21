import base64
import hashlib
import json
import os
from datetime import datetime, timezone

from github import Github, GithubException

HISTORY_FILE = ".streamlit/command_history.json"
MAX_ENTRIES_PER_USER = 50

_github_token: str = ""
_github_repo: str = ""


def configure(github_token: str, github_repo: str) -> None:
    global _github_token, _github_repo
    _github_token = github_token or ""
    _github_repo = github_repo or ""


def _email_hash(email: str) -> str:
    return hashlib.sha256(email.encode()).hexdigest()[:16]


def _gh_path(email_hash: str) -> str:
    return f"_tftool/history_{email_hash}.json"


def _load_from_github(email_hash: str) -> list[dict]:
    if not _github_token or not _github_repo:
        return []
    try:
        g = Github(_github_token)
        repo = g.get_repo(_github_repo)
        contents = repo.get_contents(_gh_path(email_hash))
        raw = base64.b64decode(contents.content).decode("utf-8")
        return json.loads(raw)
    except GithubException as e:
        if e.status == 404:
            return []
        return []
    except Exception:
        return []


def _save_to_github(email_hash: str, entries: list[dict]) -> None:
    if not _github_token or not _github_repo:
        return
    try:
        g = Github(_github_token)
        repo = g.get_repo(_github_repo)
        path = _gh_path(email_hash)
        content = json.dumps(entries, indent=2)
        message = "chore: update command history"
        try:
            existing = repo.get_contents(path)
            repo.update_file(path, message, content, existing.sha)
        except GithubException as e:
            if e.status == 404:
                repo.create_file(path, message, content)
    except Exception:
        pass


def _load_local() -> dict:
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_local(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def add_entry(email: str, user_input: str, intent: dict) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "input": user_input,
        "operation_type": intent.get("operation_type", ""),
        "resource_type": intent.get("resource_type", ""),
        "resource_name": intent.get("resource_name", ""),
    }

    if _github_token and _github_repo:
        email_hash = _email_hash(email)
        entries = _load_from_github(email_hash)
        if entries and entries[0].get("input") == user_input:
            return
        entries.insert(0, entry)
        entries = entries[:MAX_ENTRIES_PER_USER]
        _save_to_github(email_hash, entries)
    else:
        data = _load_local()
        entries = data.get(email, [])
        if entries and entries[0].get("input") == user_input:
            return
        entries.insert(0, entry)
        data[email] = entries[:MAX_ENTRIES_PER_USER]
        _save_local(data)


def get_entries(email: str) -> list[dict]:
    if _github_token and _github_repo:
        return _load_from_github(_email_hash(email))
    return _load_local().get(email, [])
