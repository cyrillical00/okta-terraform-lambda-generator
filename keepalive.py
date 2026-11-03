"""Weekly keepalive for the Okta and GCP credentials behind live env-context.

Run by .github/workflows/keepalive.yml every Monday. Each run makes one cheap,
read-only call per provider with the same secrets the Streamlit app uses:

  okta  GET /api/v1/groups?limit=1 with OKTA_API_TOKEN. Okta revokes SSWS
        tokens after 30 days without use and every call resets that clock,
        so a weekly run gives four chances per window. Integrator Free orgs
        are also reported to be deactivated after ~180 days without use.

  gcp   Mint an access token from GCP_SA_JSON, then list one service account
        (the same IAM API the app's env-context uses). GCP keys don't expire
        when idle, so this leg is a canary: it goes red if the key was
        deleted, disabled (leaked-key auto-disable included), or lost its
        IAM role.

Both checks always run. The process exits 1 if either fails, which turns the
workflow red and makes GitHub email the owner.

The repo is public, so Actions logs are too. Output is limited to provider,
pass/fail, HTTP status and the provider's error code. No org URL, project id,
resource names, exception messages or response bodies are printed.

Usage:
    python keepalive.py           # both providers
    python keepalive.py okta      # one provider
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

import requests

TIMEOUT_S = 15
GCP_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


@dataclass
class Result:
    provider: str
    ok: bool
    detail: str


def _missing(*names: str) -> list[str]:
    return [n for n in names if not os.environ.get(n, "").strip()]


def _okta_error_code(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    code = body.get("errorCode", "")
    summary = body.get("errorSummary", "")
    return f"{code}: {summary}".strip(": ")


_OKTA_HINTS = {
    401: "token expired (30 days idle) or revoked. Mint a new one under "
         "Security > API > Tokens, then update OKTA_API_TOKEN in Streamlit "
         "Cloud and GitHub secrets.",
    403: "token works but its admin lacks read access to groups.",
    404: "OKTA_ORG_URL is wrong or the org was deactivated.",
    429: "rate limited; transient, rerun the workflow later.",
}
_OKTA_REDIRECT_HINT = "redirected; the org may be deactivated or OKTA_ORG_URL is wrong."


def check_okta() -> Result:
    missing = _missing("OKTA_ORG_URL", "OKTA_API_TOKEN")
    if missing:
        return Result("okta", False, f"secret(s) not set: {', '.join(missing)}")

    base = os.environ["OKTA_ORG_URL"].strip().rstrip("/")
    token = os.environ["OKTA_API_TOKEN"].strip()
    try:
        resp = requests.get(
            f"{base}/api/v1/groups",
            params={"limit": 1},
            headers={"Authorization": f"SSWS {token}", "Accept": "application/json"},
            timeout=TIMEOUT_S,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        # Exception text can carry the org URL; print the type only.
        return Result("okta", False, f"request failed: {type(exc).__name__}")

    if resp.status_code == 200:
        # A real groups call returns a JSON list. Anything else on a 200 (an
        # HTML error page, say) means we didn't reach a live org API.
        try:
            is_list = isinstance(resp.json(), list)
        except ValueError:
            is_list = False
        if is_list:
            return Result("okta", True, "HTTP 200, token idle clock reset")
        return Result(
            "okta", False,
            "HTTP 200 but not a JSON list; the org may be deactivated or "
            "OKTA_ORG_URL points somewhere else.",
        )

    parts = [f"HTTP {resp.status_code}"]
    code = _okta_error_code(resp)
    if code:
        parts.append(code)
    hint = _OKTA_HINTS.get(resp.status_code)
    if hint is None and 300 <= resp.status_code < 400:
        hint = _OKTA_REDIRECT_HINT
    if hint:
        parts.append(hint)
    return Result("okta", False, " | ".join(parts))


def _gcp_error_status(resp) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    err = body.get("error") if isinstance(body, dict) else None
    return err.get("status", "") if isinstance(err, dict) else ""


def check_gcp() -> Result:
    missing = _missing("GCP_PROJECT_ID", "GCP_SA_JSON")
    if missing:
        return Result("gcp", False, f"secret(s) not set: {', '.join(missing)}")

    try:
        from google.auth.exceptions import GoogleAuthError, RefreshError
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
    except ImportError:
        return Result("gcp", False, "google-auth is not installed")

    project = os.environ["GCP_PROJECT_ID"].strip()
    # Same BOM strip as gcp_client._build_credentials; Notepad and
    # PowerShell pastes often carry one.
    raw = os.environ["GCP_SA_JSON"].lstrip("\ufeff").strip()
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        return Result("gcp", False, "GCP_SA_JSON is not valid JSON")
    if not isinstance(info, dict):
        # e.g. a key pasted with an extra layer of string quoting
        return Result("gcp", False, "GCP_SA_JSON is JSON but not an object")
    try:
        creds = service_account.Credentials.from_service_account_info(info, scopes=GCP_SCOPES)
    except (KeyError, ValueError):
        return Result("gcp", False, "GCP_SA_JSON is not a usable service-account key")

    session = AuthorizedSession(creds)
    try:
        resp = session.get(
            f"https://iam.googleapis.com/v1/projects/{project}/serviceAccounts",
            params={"pageSize": 1},
            timeout=TIMEOUT_S,
        )
    except RefreshError:
        return Result(
            "gcp", False,
            "key rejected at token mint: deleted, disabled, or expired. "
            "Check IAM > Service accounts > Keys, then update GCP_SA_JSON in "
            "Streamlit Cloud and GitHub secrets.",
        )
    except (GoogleAuthError, requests.RequestException) as exc:
        return Result("gcp", False, f"request failed: {type(exc).__name__}")

    if resp.status_code == 200:
        return Result("gcp", True, "HTTP 200, key minted a token and IAM read works")

    parts = [f"HTTP {resp.status_code}"]
    status = _gcp_error_status(resp)
    if status:
        parts.append(status)
    hint = _GCP_HINTS.get(resp.status_code)
    if hint:
        parts.append(hint)
    return Result("gcp", False, " | ".join(parts))


_GCP_HINTS = {
    401: "access token rejected; the key may have just been disabled.",
    403: "key is valid but the service account lacks iam.serviceAccounts.list "
         "or the IAM API is disabled.",
    404: "GCP_PROJECT_ID is wrong or the project was deleted.",
    429: "rate limited; transient, rerun the workflow later.",
}


CHECKS = {"okta": check_okta, "gcp": check_gcp}


def _run(name: str) -> Result:
    # One provider's surprise exception must not hide the other's result.
    # Type name only: exception text can carry header or key material.
    try:
        return CHECKS[name]()
    except Exception as exc:
        return Result(name, False, f"unexpected {type(exc).__name__}")


def main(argv: list[str]) -> int:
    names = argv or list(CHECKS)
    unknown = [n for n in names if n not in CHECKS]
    if unknown:
        print(f"unknown provider(s): {', '.join(unknown)}; choose from {', '.join(CHECKS)}")
        return 2

    results = [_run(n) for n in names]
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for r in results:
        print(f"{r.provider}: {'OK' if r.ok else 'FAIL'} ({r.detail})")
        if in_actions and not r.ok:
            print(f"::error title={r.provider} keepalive failed::{r.detail}")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
