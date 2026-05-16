"""Secret rotation tracking + admin-facing staleness warnings.

Stores last-rotation dates in `_tftool/secret_rotation.json` (admin-edited,
GitHub-backed). Compares each entry against a per-secret target cadence and
returns the list of stale secrets so the sidebar can warn admins.

Side-effect free at module load. Configure once via `secret_rotation.configure`.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone

_GH_PATH = "_tftool/secret_rotation.json"
_LOCAL_PATH = ".streamlit/secret_rotation.json"

# Target rotation cadence per secret name (days). Treat anything older than
# this as stale and warn admins.
TARGET_CADENCE_DAYS = {
    # Streamlit + CLI + every headless surface
    "ANTHROPIC_API_KEY": 90,
    "GITHUB_TOKEN": 90,
    # Streamlit + CLI live-context provider creds
    "OKTA_API_TOKEN": 180,
    "AWS_ACCESS_KEY_ID": 90,
    "AWS_SECRET_ACCESS_KEY": 90,
    "GCP_SA_JSON": 180,
    # Phase 10 Track 2 headless surfaces (HTTP, Slack, JIRA).
    # Slack signing secrets and JIRA webhook secrets are typically
    # longer-lived because rotation requires touching the third party
    # (Slack admin console, JIRA Automation rule). API tokens that the
    # tool fully owns rotate on the same 90-day cadence as the
    # Anthropic key.
    "TFGEN_API_KEY": 90,
    "SLACK_SIGNING_SECRET": 180,
    "JIRA_WEBHOOK_SECRET": 180,
    "JIRA_API_TOKEN": 90,
    # Phase 11, JAMF Pro provider live-context credentials.
    # JAMF Pro API roles + integrations expose oauth2 client_id +
    # client_secret. Treat them as 90-day rotators like other API
    # credentials we own end to end.
    "JAMF_CLIENT_ID": 90,
    "JAMF_CLIENT_SECRET": 90,
    # Phase 13, Fleet MDM credentials. Same env vars cover both the GitOps
    # (fleetctl apply) and the Terraform (l-teles/fleetdm provider) paths.
    # 90-day cadence matches other API tokens we own end to end.
    "FLEET_URL": 90,
    "FLEET_API_TOKEN": 90,
    # Phase 15, Snowflake credentials. Key-pair auth required (Snowflake
    # deprecated password auth Nov 2025). Same 90-day cadence as the other
    # provider credentials we own end to end.
    "SNOWFLAKE_ACCOUNT": 90,
    "SNOWFLAKE_USER": 90,
    "SNOWFLAKE_PRIVATE_KEY": 90,
    "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE": 90,
    "SNOWFLAKE_ROLE": 90,
    "SNOWFLAKE_WAREHOUSE": 90,
}

_github_token: str = ""
_github_repo: str = ""


def configure(github_token: str, github_repo: str) -> None:
    global _github_token, _github_repo
    _github_token = (github_token or "").strip()
    _github_repo = (github_repo or "").strip()


def _load() -> dict:
    """Load the rotation map. Format: { "SECRET_NAME": "YYYY-MM-DD", ... }"""
    if _github_token and _github_repo:
        try:
            from github import Github, GithubException
            g = Github(_github_token)
            repo = g.get_repo(_github_repo)
            try:
                contents = repo.get_contents(_GH_PATH)
                return json.loads(base64.b64decode(contents.content).decode("utf-8"))
            except GithubException as e:
                if e.status == 404:
                    return {}
                raise
        except Exception:
            return {}
    try:
        with open(_LOCAL_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    body = json.dumps(data, indent=2)
    if _github_token and _github_repo:
        try:
            from github import Github, GithubException
            g = Github(_github_token)
            repo = g.get_repo(_github_repo)
            try:
                existing = repo.get_contents(_GH_PATH)
                repo.update_file(_GH_PATH, "chore: update secret rotation dates", body, existing.sha)
            except GithubException as e:
                if e.status == 404:
                    repo.create_file(_GH_PATH, "chore: create secret rotation dates", body)
                else:
                    raise
            return
        except Exception:
            pass
    try:
        os.makedirs(os.path.dirname(_LOCAL_PATH), exist_ok=True)
        with open(_LOCAL_PATH, "w", encoding="utf-8") as f:
            f.write(body)
    except OSError:
        pass


def get_dates() -> dict[str, str]:
    """Return the current rotation-date map."""
    return _load()


def set_date(secret_name: str, iso_date: str) -> None:
    """Record a new rotation date for `secret_name`. iso_date is YYYY-MM-DD."""
    data = _load()
    data[secret_name] = iso_date
    _save(data)


def stale_list(now: datetime | None = None) -> list[dict]:
    """Return the list of secrets that are stale (older than their target
    cadence). Each entry: { name, last_rotated, age_days, target_days }.
    Secrets with no recorded date are returned with age_days = None and a
    warning that they have never been recorded.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    data = _load()
    out: list[dict] = []
    for name, target in TARGET_CADENCE_DAYS.items():
        last = data.get(name)
        if not last:
            out.append({
                "name": name,
                "last_rotated": None,
                "age_days": None,
                "target_days": target,
                "stale": True,
                "reason": "never recorded",
            })
            continue
        try:
            last_dt = datetime.fromisoformat(last).replace(tzinfo=timezone.utc) \
                if "T" not in last else datetime.fromisoformat(last)
        except ValueError:
            out.append({
                "name": name,
                "last_rotated": last,
                "age_days": None,
                "target_days": target,
                "stale": True,
                "reason": "invalid date format",
            })
            continue
        age = (now - last_dt).days
        if age > target:
            out.append({
                "name": name,
                "last_rotated": last,
                "age_days": age,
                "target_days": target,
                "stale": True,
                "reason": f"older than {target}-day target",
            })
    return out
