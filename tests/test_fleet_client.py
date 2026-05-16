"""Tests for `fleet_client.FleetClient`.

Standalone-runnable: `python tests/test_fleet_client.py`.

Mocks `requests.Session.get` to avoid hitting a real Fleet instance.
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

from fleet_client import (
    FleetClient,
    FleetAuthError,
    FleetError,
    FleetParseError,
    FleetServerError,
)


def _mock_response(status: int = 200, json_payload: dict | None = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.ok = 200 <= status < 400
    resp.text = text or (json.dumps(json_payload) if json_payload is not None else "")
    if json_payload is None:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = json_payload
    return resp


def test_init_rejects_empty_url_and_token():
    try:
        FleetClient("", "token")
    except FleetError as e:
        assert "url is required" in str(e)
    else:
        assert False, "expected FleetError for empty url"
    try:
        FleetClient("https://fleet.example.com", "")
    except FleetError as e:
        assert "api_token is required" in str(e)
    else:
        assert False, "expected FleetError for empty token"


def test_list_labels_success():
    payload = {"labels": [{"id": 14, "name": "Engineering"}, {"id": 22, "name": "C-Suite"}]}
    client = FleetClient("https://fleet.example.com/", "secret-token")
    with patch.object(client.session, "get", return_value=_mock_response(200, payload)) as mock_get:
        labels = client.list_labels()
        # Confirm bearer header and the correct URL.
        called_url, called_kwargs = mock_get.call_args.args[0], mock_get.call_args.kwargs
        assert called_url == "https://fleet.example.com/api/v1/fleet/labels"
        assert called_kwargs["headers"]["Authorization"] == "Bearer secret-token"
    assert labels == payload["labels"]


def test_auth_401_raises_fleet_auth_error():
    client = FleetClient("https://fleet.example.com", "bad-token")
    with patch.object(client.session, "get", return_value=_mock_response(401, text="unauthorized")):
        try:
            client.list_labels()
        except FleetAuthError as e:
            assert "rejected the token" in str(e)
        else:
            assert False, "expected FleetAuthError on 401"


def test_5xx_raises_fleet_server_error():
    client = FleetClient("https://fleet.example.com", "tok")
    with patch.object(client.session, "get", return_value=_mock_response(503, text="upstream timeout")):
        try:
            client.list_policies()
        except FleetServerError as e:
            assert "503" in str(e)
        else:
            assert False, "expected FleetServerError on 503"


def test_timeout_raises_fleet_server_error():
    client = FleetClient("https://fleet.example.com", "tok")
    with patch.object(client.session, "get", side_effect=requests.Timeout("read timed out")):
        try:
            client.list_queries()
        except FleetServerError as e:
            assert "timed out" in str(e)
        else:
            assert False, "expected FleetServerError on timeout"


def test_malformed_json_raises_fleet_parse_error():
    client = FleetClient("https://fleet.example.com", "tok")
    with patch.object(client.session, "get", return_value=_mock_response(200, json_payload=None, text="<html>")):
        try:
            client.list_teams()
        except FleetParseError as e:
            assert "not JSON" in str(e)
        else:
            assert False, "expected FleetParseError on malformed JSON"


def test_empty_list_response():
    """Fleet returns {"labels": []} for an org with no labels. Client should
    surface an empty list, not raise."""
    client = FleetClient("https://fleet.example.com", "tok")
    with patch.object(client.session, "get", return_value=_mock_response(200, {"labels": []})):
        assert client.list_labels() == []


if __name__ == "__main__":
    import traceback
    failures = []
    tests = [
        test_init_rejects_empty_url_and_token,
        test_list_labels_success,
        test_auth_401_raises_fleet_auth_error,
        test_5xx_raises_fleet_server_error,
        test_timeout_raises_fleet_server_error,
        test_malformed_json_raises_fleet_parse_error,
        test_empty_list_response,
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
