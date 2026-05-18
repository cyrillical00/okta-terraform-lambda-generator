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
        assert called_url == "https://fleet.example.com/api/v1/fleet/labels?page=0&per_page=100"
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


# ---------------------------------------------------------------------------
# Phase 19b: pagination + per-team policies
# Test fixtures use synthetic low-entropy tokens to avoid tripping gitleaks /
# GitHub push-protection scanners while still matching the redact regex.
# ---------------------------------------------------------------------------

_FAKE_TOKEN = "FLEET-TOKEN-EXAMPLE-" + "a" * 32


def test_pagination_happy_path():
    """3 pages of labels: 100 + 100 + 50 = 250 total, 3 HTTP calls."""
    client = FleetClient("https://fleet.example.com", _FAKE_TOKEN)
    page_full_a = {"labels": [{"id": i, "name": f"label-{i}"} for i in range(100)]}
    page_full_b = {"labels": [{"id": 100 + i, "name": f"label-{100 + i}"} for i in range(100)]}
    page_partial = {"labels": [{"id": 200 + i, "name": f"label-{200 + i}"} for i in range(50)]}
    responses = [
        _mock_response(200, page_full_a),
        _mock_response(200, page_full_b),
        _mock_response(200, page_partial),
    ]
    with patch.object(client.session, "get", side_effect=responses) as mock_get:
        labels = client.list_labels()
    assert len(labels) == 250
    assert mock_get.call_count == 3
    # First call should request page=0; last call should request page=2.
    first_url = mock_get.call_args_list[0].args[0]
    last_url = mock_get.call_args_list[2].args[0]
    assert "page=0" in first_url and "per_page=100" in first_url
    assert "page=2" in last_url and "per_page=100" in last_url


def test_pagination_empty_page_termination():
    """A first page that is shorter than per_page terminates after 1 HTTP call."""
    client = FleetClient("https://fleet.example.com", _FAKE_TOKEN)
    payload = {"labels": [{"id": i, "name": f"l-{i}"} for i in range(50)]}
    with patch.object(client.session, "get", return_value=_mock_response(200, payload)) as mock_get:
        labels = client.list_labels()
    assert len(labels) == 50
    assert mock_get.call_count == 1


def test_pagination_safety_cap():
    """100 pages of 100 items each would loop forever; the safety cap must
    abort with FleetServerError instead."""
    client = FleetClient("https://fleet.example.com", _FAKE_TOKEN)
    full_page = {"labels": [{"id": i, "name": f"l-{i}"} for i in range(100)]}
    # Infinite stream of full pages; side_effect can be a callable.
    with patch.object(client.session, "get", return_value=_mock_response(200, full_page)) as mock_get:
        try:
            client.list_labels()
        except FleetServerError as e:
            assert "safety cap" in str(e)
            # MAX_PAGES is 100, so exactly 100 HTTP calls should have been made
            # before the cap fires.
            assert mock_get.call_count == FleetClient.MAX_PAGES
        else:
            assert False, "expected FleetServerError when safety cap is hit"


def test_list_team_policies_happy_path():
    """list_team_policies(1) hits /api/v1/fleet/teams/1/policies?page=0&per_page=100."""
    client = FleetClient("https://fleet.example.com", _FAKE_TOKEN)
    payload = {"policies": [{"id": 7, "name": "Disk encryption required"}]}
    with patch.object(client.session, "get", return_value=_mock_response(200, payload)) as mock_get:
        result = client.list_team_policies(1)
    assert result == payload["policies"]
    called_url = mock_get.call_args.args[0]
    assert called_url == "https://fleet.example.com/api/v1/fleet/teams/1/policies?page=0&per_page=100"


def test_list_team_policies_404_returns_empty():
    """Fleet Free returns 404 on the per-team endpoint. Client should
    downgrade to an empty list, not raise, so a single team failure does not
    abort the whole env-context fetch."""
    client = FleetClient("https://fleet.example.com", _FAKE_TOKEN)
    with patch.object(client.session, "get", return_value=_mock_response(404, text="not found")):
        result = client.list_team_policies(99)
    assert result == []


def test_429_raises_fleet_server_error_with_retry_after():
    """429 with a Retry-After header should surface a FleetServerError that
    includes the retry hint."""
    client = FleetClient("https://fleet.example.com", _FAKE_TOKEN)
    resp = _mock_response(429, text="too many requests")
    resp.headers = {"Retry-After": "30"}
    with patch.object(client.session, "get", return_value=resp):
        try:
            client.list_labels()
        except FleetServerError as e:
            assert "429" in str(e)
            assert "Retry-After: 30" in str(e)
        else:
            assert False, "expected FleetServerError on 429"


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
        test_pagination_happy_path,
        test_pagination_empty_page_termination,
        test_pagination_safety_cap,
        test_list_team_policies_happy_path,
        test_list_team_policies_404_returns_empty,
        test_429_raises_fleet_server_error_with_retry_after,
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
