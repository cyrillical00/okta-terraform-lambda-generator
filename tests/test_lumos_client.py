"""Tests for `lumos_client.LumosClient`.

Standalone-runnable: `python tests/test_lumos_client.py`.

Mocks `requests.Session.get` to avoid hitting a real Lumos tenant.
Synthetic low-entropy tokens (`lsk-EXAMPLE-*`) to avoid tripping gitleaks
/ GitHub push-protection scanners while still matching the redact regex
for `lsk_*` Lumos PATs.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import requests

from lumos_client import (
    LumosClient,
    LumosAuthError,
    LumosError,
    LumosNotFoundError,
    LumosParseError,
    LumosServerError,
)


_FAKE_TOKEN = "lsk-EXAMPLE-" + "a" * 32
_FAKE_BASE = "https://api.lumos.com"


def _mock_response(status: int = 200, json_payload=None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.ok = 200 <= status < 400
    if text:
        resp.text = text
    elif json_payload is not None:
        resp.text = json.dumps(json_payload)
    else:
        resp.text = ""
    if json_payload is None:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = json_payload
    resp.headers = {}
    return resp


def test_init_rejects_empty_token():
    try:
        LumosClient("")
    except LumosError as e:
        assert "api_token is required" in str(e)
    else:
        assert False, "expected LumosError for empty token"


def test_default_base_url():
    """When no base_url is passed, default to https://api.lumos.com."""
    client = LumosClient(_FAKE_TOKEN)
    assert client.base == "https://api.lumos.com"


def test_list_apps_success_wrapped():
    """Wrapper-key response shape: {"results": [...]}."""
    payload = {"results": [{"id": "app-1", "name": "Slack"}, {"id": "app-2", "name": "Notion"}]}
    client = LumosClient(_FAKE_TOKEN)
    with patch.object(client.session, "get", return_value=_mock_response(200, payload)) as mock_get:
        apps = client.list_apps()
        called_url = mock_get.call_args.args[0]
        called_kwargs = mock_get.call_args.kwargs
        assert called_url == f"{_FAKE_BASE}/apps"
        assert called_kwargs["headers"]["Authorization"] == f"Bearer {_FAKE_TOKEN}"
    assert apps == payload["results"]


def test_list_groups_bare_array_response():
    """Some Lumos list endpoints may return a bare JSON array. The client
    must accept that shape, not raise LumosParseError."""
    payload = [{"id": "grp-1", "name": "Engineering"}]
    client = LumosClient(_FAKE_TOKEN)
    with patch.object(client.session, "get", return_value=_mock_response(200, payload)):
        groups = client.list_groups()
    assert groups == payload


def test_auth_401_raises_lumos_auth_error():
    client = LumosClient("bad-token")
    with patch.object(client.session, "get", return_value=_mock_response(401, text="unauthorized")):
        try:
            client.list_apps()
        except LumosAuthError as e:
            assert "rejected the token" in str(e)
        else:
            assert False, "expected LumosAuthError on 401"


def test_5xx_raises_lumos_server_error():
    client = LumosClient(_FAKE_TOKEN)
    with patch.object(client.session, "get", return_value=_mock_response(503, text="upstream timeout")):
        try:
            client.list_requestable_permissions()
        except LumosServerError as e:
            assert "503" in str(e)
        else:
            assert False, "expected LumosServerError on 503"


def test_timeout_raises_lumos_server_error():
    client = LumosClient(_FAKE_TOKEN)
    with patch.object(client.session, "get", side_effect=requests.Timeout("read timed out")):
        try:
            client.list_groups()
        except LumosServerError as e:
            assert "timed out" in str(e)
        else:
            assert False, "expected LumosServerError on timeout"


def test_malformed_json_raises_lumos_parse_error():
    client = LumosClient(_FAKE_TOKEN)
    with patch.object(client.session, "get", return_value=_mock_response(200, json_payload=None, text="<html>")):
        try:
            client.list_apps()
        except LumosParseError as e:
            assert "not JSON" in str(e)
        else:
            assert False, "expected LumosParseError on malformed JSON"


def test_empty_list_response():
    """{"results": []} with no next_page_token should surface as []."""
    client = LumosClient(_FAKE_TOKEN)
    with patch.object(client.session, "get", return_value=_mock_response(200, {"results": []})):
        assert client.list_apps() == []


def test_pagination_happy_path_next_page_token():
    """Cursor pagination: page 1 yields next_page_token, page 2 does not.
    Verify both pages are concatenated and the token is passed as
    ?page_token=<value> on the second request."""
    client = LumosClient(_FAKE_TOKEN)
    page_one = {
        "results": [{"id": "app-1"}, {"id": "app-2"}],
        "next_page_token": "opaque-cursor-blob-abc123",
    }
    page_two = {
        "results": [{"id": "app-3"}],
        # next_page_token absent / None signals last page.
    }
    responses = [
        _mock_response(200, page_one),
        _mock_response(200, page_two),
    ]
    with patch.object(client.session, "get", side_effect=responses) as mock_get:
        apps = client.list_apps()
    assert len(apps) == 3
    assert mock_get.call_count == 2
    first_url = mock_get.call_args_list[0].args[0]
    second_url = mock_get.call_args_list[1].args[0]
    assert first_url == f"{_FAKE_BASE}/apps"
    assert "page_token=opaque-cursor-blob-abc123" in second_url


def test_pagination_safety_cap():
    """A buggy backend that returns next_page_token on every page would loop
    forever; the safety cap must abort with LumosServerError after MAX_PAGES."""
    client = LumosClient(_FAKE_TOKEN)
    looping_page = {
        "results": [{"id": "app-x"}],
        "next_page_token": "never-ends",
    }
    with patch.object(client.session, "get", return_value=_mock_response(200, looping_page)) as mock_get:
        try:
            client.list_apps()
        except LumosServerError as e:
            assert "safety cap" in str(e)
            assert mock_get.call_count == LumosClient.MAX_PAGES
        else:
            assert False, "expected LumosServerError when safety cap is hit"


def test_429_raises_lumos_server_error_with_retry_after():
    """429 with a Retry-After header should surface a LumosServerError that
    includes the retry hint."""
    client = LumosClient(_FAKE_TOKEN)
    resp = _mock_response(429, text="too many requests")
    resp.headers = {"Retry-After": "60"}
    with patch.object(client.session, "get", return_value=resp):
        try:
            client.list_apps()
        except LumosServerError as e:
            assert "429" in str(e)
            assert "Retry-After: 60" in str(e)
        else:
            assert False, "expected LumosServerError on 429"


def test_404_raises_lumos_not_found_error():
    client = LumosClient(_FAKE_TOKEN)
    with patch.object(client.session, "get", return_value=_mock_response(404, text="not found")):
        try:
            client.list_requestable_permissions()
        except LumosNotFoundError as e:
            assert "404" in str(e)
        else:
            assert False, "expected LumosNotFoundError on 404"


if __name__ == "__main__":
    import traceback
    failures = []
    tests = [
        test_init_rejects_empty_token,
        test_default_base_url,
        test_list_apps_success_wrapped,
        test_list_groups_bare_array_response,
        test_auth_401_raises_lumos_auth_error,
        test_5xx_raises_lumos_server_error,
        test_timeout_raises_lumos_server_error,
        test_malformed_json_raises_lumos_parse_error,
        test_empty_list_response,
        test_pagination_happy_path_next_page_token,
        test_pagination_safety_cap,
        test_429_raises_lumos_server_error_with_retry_after,
        test_404_raises_lumos_not_found_error,
    ]
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception:
            failures.append(t.__name__)
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
