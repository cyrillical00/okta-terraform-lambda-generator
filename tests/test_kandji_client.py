"""Tests for `kandji_client.KandjiClient`.

Standalone-runnable: `python tests/test_kandji_client.py`.

Mocks `requests.Session.get` to avoid hitting a real Kandji instance.
Synthetic low-entropy tokens to avoid tripping gitleaks / GitHub
push-protection scanners while still matching the redact regex.
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

from kandji_client import (
    KandjiClient,
    KandjiAuthError,
    KandjiError,
    KandjiNotFoundError,
    KandjiParseError,
    KandjiServerError,
)


_FAKE_TOKEN = "KANDJI-TOKEN-EXAMPLE-" + "a" * 32
_FAKE_URL = "https://example.api.kandji.io"


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


def test_init_rejects_empty_base_url_and_token():
    try:
        KandjiClient("", _FAKE_TOKEN)
    except KandjiError as e:
        assert "base_url is required" in str(e)
    else:
        assert False, "expected KandjiError for empty base_url"
    try:
        KandjiClient(_FAKE_URL, "")
    except KandjiError as e:
        assert "api_token is required" in str(e)
    else:
        assert False, "expected KandjiError for empty token"


def test_list_blueprints_success_wrapped():
    """Wrapper-key response shape: {"results": [...]}."""
    payload = {"results": [{"id": "bp-1", "name": "Sales Mac"}, {"id": "bp-2", "name": "Eng Mac"}]}
    client = KandjiClient(_FAKE_URL + "/", _FAKE_TOKEN)
    with patch.object(client.session, "get", return_value=_mock_response(200, payload)) as mock_get:
        bps = client.list_blueprints()
        called_url = mock_get.call_args.args[0]
        called_kwargs = mock_get.call_args.kwargs
        assert called_url == f"{_FAKE_URL}/api/v1/blueprints?offset=0&limit=300"
        assert called_kwargs["headers"]["Authorization"] == f"Bearer {_FAKE_TOKEN}"
    assert bps == payload["results"]


def test_list_tags_bare_array_response():
    """Some Kandji list endpoints return a bare JSON array. The client must
    accept that shape, not raise KandjiParseError."""
    payload = [{"id": "tag-1", "name": "executives"}]
    client = KandjiClient(_FAKE_URL, _FAKE_TOKEN)
    with patch.object(client.session, "get", return_value=_mock_response(200, payload)):
        tags = client.list_tags()
    assert tags == payload


def test_auth_401_raises_kandji_auth_error():
    client = KandjiClient(_FAKE_URL, "bad-token")
    with patch.object(client.session, "get", return_value=_mock_response(401, text="unauthorized")):
        try:
            client.list_blueprints()
        except KandjiAuthError as e:
            assert "rejected the token" in str(e)
        else:
            assert False, "expected KandjiAuthError on 401"


def test_5xx_raises_kandji_server_error():
    client = KandjiClient(_FAKE_URL, _FAKE_TOKEN)
    with patch.object(client.session, "get", return_value=_mock_response(503, text="upstream timeout")):
        try:
            client.list_library_items()
        except KandjiServerError as e:
            assert "503" in str(e)
        else:
            assert False, "expected KandjiServerError on 503"


def test_timeout_raises_kandji_server_error():
    client = KandjiClient(_FAKE_URL, _FAKE_TOKEN)
    with patch.object(client.session, "get", side_effect=requests.Timeout("read timed out")):
        try:
            client.list_tags()
        except KandjiServerError as e:
            assert "timed out" in str(e)
        else:
            assert False, "expected KandjiServerError on timeout"


def test_malformed_json_raises_kandji_parse_error():
    client = KandjiClient(_FAKE_URL, _FAKE_TOKEN)
    with patch.object(client.session, "get", return_value=_mock_response(200, json_payload=None, text="<html>")):
        try:
            client.list_blueprints()
        except KandjiParseError as e:
            assert "not JSON" in str(e)
        else:
            assert False, "expected KandjiParseError on malformed JSON"


def test_empty_list_response():
    """{"results": []} should surface as an empty list, not raise."""
    client = KandjiClient(_FAKE_URL, _FAKE_TOKEN)
    with patch.object(client.session, "get", return_value=_mock_response(200, {"results": []})):
        assert client.list_blueprints() == []


def test_pagination_happy_path():
    """3 pages: 300 + 300 + 50 = 650 total, 3 HTTP calls. Offsets: 0, 300, 600."""
    client = KandjiClient(_FAKE_URL, _FAKE_TOKEN)
    page_full_a = {"results": [{"id": f"bp-{i}"} for i in range(300)]}
    page_full_b = {"results": [{"id": f"bp-{300 + i}"} for i in range(300)]}
    page_partial = {"results": [{"id": f"bp-{600 + i}"} for i in range(50)]}
    responses = [
        _mock_response(200, page_full_a),
        _mock_response(200, page_full_b),
        _mock_response(200, page_partial),
    ]
    with patch.object(client.session, "get", side_effect=responses) as mock_get:
        bps = client.list_blueprints()
    assert len(bps) == 650
    assert mock_get.call_count == 3
    first_url = mock_get.call_args_list[0].args[0]
    last_url = mock_get.call_args_list[2].args[0]
    assert "offset=0" in first_url and "limit=300" in first_url
    assert "offset=600" in last_url and "limit=300" in last_url


def test_pagination_safety_cap():
    """100 pages of 300 items each would loop forever; the safety cap must
    abort with KandjiServerError after MAX_PAGES requests."""
    client = KandjiClient(_FAKE_URL, _FAKE_TOKEN)
    full_page = {"results": [{"id": f"bp-{i}"} for i in range(300)]}
    with patch.object(client.session, "get", return_value=_mock_response(200, full_page)) as mock_get:
        try:
            client.list_blueprints()
        except KandjiServerError as e:
            assert "safety cap" in str(e)
            assert mock_get.call_count == KandjiClient.MAX_PAGES
        else:
            assert False, "expected KandjiServerError when safety cap is hit"


def test_429_raises_kandji_server_error_with_retry_after():
    """429 with a Retry-After header should surface a KandjiServerError that
    includes the retry hint (Kandji caps at 10,000 req/hr)."""
    client = KandjiClient(_FAKE_URL, _FAKE_TOKEN)
    resp = _mock_response(429, text="too many requests")
    resp.headers = {"Retry-After": "120"}
    with patch.object(client.session, "get", return_value=resp):
        try:
            client.list_blueprints()
        except KandjiServerError as e:
            assert "429" in str(e)
            assert "Retry-After: 120" in str(e)
        else:
            assert False, "expected KandjiServerError on 429"


def test_404_raises_kandji_not_found_error():
    client = KandjiClient(_FAKE_URL, _FAKE_TOKEN)
    with patch.object(client.session, "get", return_value=_mock_response(404, text="not found")):
        try:
            client.list_tags()
        except KandjiNotFoundError as e:
            assert "404" in str(e)
        else:
            assert False, "expected KandjiNotFoundError on 404"


if __name__ == "__main__":
    import traceback
    failures = []
    tests = [
        test_init_rejects_empty_base_url_and_token,
        test_list_blueprints_success_wrapped,
        test_list_tags_bare_array_response,
        test_auth_401_raises_kandji_auth_error,
        test_5xx_raises_kandji_server_error,
        test_timeout_raises_kandji_server_error,
        test_malformed_json_raises_kandji_parse_error,
        test_empty_list_response,
        test_pagination_happy_path,
        test_pagination_safety_cap,
        test_429_raises_kandji_server_error_with_retry_after,
        test_404_raises_kandji_not_found_error,
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
