"""Tests for keepalive.py (weekly Okta + GCP credential ping).

Standalone-runnable: `python -m pytest tests/test_keepalive.py`.

No network: requests.get and the google-auth session are patched. The
public-log guarantees get explicit tests, since Actions logs on this repo
are world-readable.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import pytest
import requests

import keepalive

ORG_URL = "https://example-org.okta.com"
OKTA_TOKEN = "00" + "x" * 40
PROJECT = "example-project-123"
SA_JSON = json.dumps({"type": "service_account", "client_email": "sa@example-project-123.iam.gserviceaccount.com"})


def _resp(status: int, body=None):
    r = MagicMock()
    r.status_code = status
    if body is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = body
    return r


@pytest.fixture
def env(monkeypatch):
    for name in ("OKTA_ORG_URL", "OKTA_API_TOKEN", "GCP_PROJECT_ID", "GCP_SA_JSON", "GITHUB_ACTIONS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


_GOOGLE_TREES = ("google.auth", "google.oauth2")


def _google_keys():
    return [k for k in sys.modules if any(k == t or k.startswith(t + ".") for t in _GOOGLE_TREES)]


@pytest.fixture
def real_google():
    """Swap the real google-auth modules in for one test, then restore.

    tests/test_gcp_client.py installs plain-module fakes for google.auth and
    google.oauth2 at import time; pytest collects it first, so without this
    keepalive's `from google.auth.transport.requests import ...` fails.
    """
    saved = {k: sys.modules.pop(k) for k in _google_keys()}
    for mod in ("google.auth.exceptions", "google.auth.transport.requests", "google.oauth2.service_account"):
        importlib.import_module(mod)
    yield
    for k in _google_keys():
        del sys.modules[k]
    sys.modules.update(saved)


def _set_okta(env):
    env.setenv("OKTA_ORG_URL", ORG_URL + "/")
    env.setenv("OKTA_API_TOKEN", OKTA_TOKEN)


def _set_gcp(env, sa_json=SA_JSON):
    env.setenv("GCP_PROJECT_ID", PROJECT)
    env.setenv("GCP_SA_JSON", sa_json)


# Okta


def test_okta_missing_secrets_fails_without_network(env):
    with patch.object(keepalive.requests, "get") as get:
        r = keepalive.check_okta()
    assert not r.ok
    assert "OKTA_ORG_URL" in r.detail and "OKTA_API_TOKEN" in r.detail
    get.assert_not_called()


def test_okta_ok_calls_groups_with_ssws(env):
    _set_okta(env)
    with patch.object(keepalive.requests, "get", return_value=_resp(200, [])) as get:
        r = keepalive.check_okta()
    assert r.ok
    url = get.call_args.args[0]
    assert url == f"{ORG_URL}/api/v1/groups"  # trailing slash stripped
    assert get.call_args.kwargs["headers"]["Authorization"] == f"SSWS {OKTA_TOKEN}"
    assert get.call_args.kwargs["params"] == {"limit": 1}


def test_okta_200_html_page_is_a_failure(env):
    _set_okta(env)
    with patch.object(keepalive.requests, "get", return_value=_resp(200)):  # .json() raises
        r = keepalive.check_okta()
    assert not r.ok
    assert "not a JSON list" in r.detail


def test_okta_401_reports_error_code_and_hint_not_token(env):
    _set_okta(env)
    body = {"errorCode": "E0000011", "errorSummary": "Invalid token provided"}
    with patch.object(keepalive.requests, "get", return_value=_resp(401, body)):
        r = keepalive.check_okta()
    assert not r.ok
    assert "HTTP 401" in r.detail and "E0000011" in r.detail and "30 days" in r.detail
    assert OKTA_TOKEN not in r.detail
    assert ORG_URL not in r.detail


def test_okta_redirect_flags_possible_deactivation(env):
    _set_okta(env)
    with patch.object(keepalive.requests, "get", return_value=_resp(302)):
        r = keepalive.check_okta()
    assert not r.ok
    assert "deactivated" in r.detail


def test_okta_network_error_prints_type_only(env):
    _set_okta(env)
    exc = requests.ConnectionError(f"HTTPSConnectionPool(host='{ORG_URL}') boom")
    with patch.object(keepalive.requests, "get", side_effect=exc):
        r = keepalive.check_okta()
    assert not r.ok
    assert r.detail == "request failed: ConnectionError"


# GCP


def test_gcp_missing_secrets(env):
    r = keepalive.check_gcp()
    assert not r.ok
    assert "GCP_PROJECT_ID" in r.detail and "GCP_SA_JSON" in r.detail


def test_gcp_bad_json(env, real_google):
    _set_gcp(env, sa_json="{not json")
    r = keepalive.check_gcp()
    assert not r.ok
    assert "not valid JSON" in r.detail


@pytest.mark.parametrize("payload", ["[1]", '"{\\"type\\": \\"service_account\\"}"', "42"])
def test_gcp_json_that_is_not_an_object(env, real_google, payload):
    _set_gcp(env, sa_json=payload)
    r = keepalive.check_gcp()
    assert not r.ok
    assert r.detail == "GCP_SA_JSON is JSON but not an object"


def _patch_google(session_get):
    """Patch credential construction + AuthorizedSession; return the session mock."""
    session = MagicMock()
    session.get.side_effect = session_get
    creds_patch = patch(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        return_value=object(),
    )
    session_patch = patch(
        "google.auth.transport.requests.AuthorizedSession", return_value=session
    )
    return creds_patch, session_patch, session


def test_gcp_ok_strips_bom_and_lists_one_sa(env, real_google):
    _set_gcp(env, sa_json="\ufeff" + SA_JSON)
    creds_patch, session_patch, session = _patch_google(lambda *a, **k: _resp(200, {"accounts": []}))
    with creds_patch as from_info, session_patch:
        r = keepalive.check_gcp()
    assert r.ok
    assert from_info.call_args.args[0]["type"] == "service_account"
    assert session.get.call_args.args[0] == (
        f"https://iam.googleapis.com/v1/projects/{PROJECT}/serviceAccounts"
    )


def test_gcp_refresh_error_means_dead_key(env, real_google):
    from google.auth.exceptions import RefreshError

    _set_gcp(env)

    def boom(*a, **k):
        raise RefreshError("invalid_grant: sa@example-project-123 key disabled")

    creds_patch, session_patch, _ = _patch_google(boom)
    with creds_patch, session_patch:
        r = keepalive.check_gcp()
    assert not r.ok
    assert "token mint" in r.detail
    assert PROJECT not in r.detail


def test_gcp_403_reports_status(env, real_google):
    _set_gcp(env)
    body = {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": f"projects/{PROJECT} denied"}}
    creds_patch, session_patch, _ = _patch_google(lambda *a, **k: _resp(403, body))
    with creds_patch, session_patch:
        r = keepalive.check_gcp()
    assert not r.ok
    assert "PERMISSION_DENIED" in r.detail
    assert PROJECT not in r.detail


def test_gcp_404_points_at_project_id(env, real_google):
    _set_gcp(env)
    body = {"error": {"code": 404, "status": "NOT_FOUND", "message": f"projects/{PROJECT} not found"}}
    creds_patch, session_patch, _ = _patch_google(lambda *a, **k: _resp(404, body))
    with creds_patch, session_patch:
        r = keepalive.check_gcp()
    assert not r.ok
    assert "NOT_FOUND" in r.detail and "GCP_PROJECT_ID" in r.detail
    assert PROJECT not in r.detail


# main


def test_main_runs_every_check_and_fails_if_any_fail(env, capsys):
    calls = []
    fake = {
        "okta": lambda: calls.append("okta") or keepalive.Result("okta", False, "x"),
        "gcp": lambda: calls.append("gcp") or keepalive.Result("gcp", True, "y"),
    }
    env.setenv("GITHUB_ACTIONS", "true")
    with patch.dict(keepalive.CHECKS, fake, clear=True):
        rc = keepalive.main([])
    assert rc == 1
    assert calls == ["okta", "gcp"]
    out = capsys.readouterr().out
    assert "okta: FAIL (x)" in out and "gcp: OK (y)" in out
    assert "::error title=okta keepalive failed::x" in out


def test_main_isolates_unexpected_exceptions(env, capsys):
    def boom():
        raise UnicodeEncodeError("latin-1", "secret\u2019value", 6, 7, "ordinal not in range")

    fake = {"okta": boom, "gcp": lambda: keepalive.Result("gcp", True, "y")}
    with patch.dict(keepalive.CHECKS, fake, clear=True):
        rc = keepalive.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "okta: FAIL (unexpected UnicodeEncodeError)" in out
    assert "gcp: OK (y)" in out
    assert "secret" not in out


def test_main_all_ok_and_single_provider(env):
    fake = {
        "okta": lambda: keepalive.Result("okta", True, "ok"),
        "gcp": lambda: pytest.fail("gcp should not run"),
    }
    with patch.dict(keepalive.CHECKS, fake, clear=True):
        assert keepalive.main(["okta"]) == 0


def test_main_unknown_provider(env):
    assert keepalive.main(["aws"]) == 2
