"""Tests for JamfClient. Standalone-runnable: python tests/test_jamf_client.py.

Exercises token minting, 401 retry/refresh, Classic XML parsing, v2 JSON parsing,
non-2xx error mapping, and fqdn normalisation.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch, MagicMock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from jamf_client import JamfClient, JamfError


def _ok_token_response(expires_in: int = 1800) -> MagicMock:
    r = MagicMock()
    r.ok = True
    r.status_code = 200
    r.json.return_value = {"access_token": "tkn-abc", "expires_in": expires_in}
    return r


def _ok_xml_response(text: str) -> MagicMock:
    r = MagicMock()
    r.ok = True
    r.status_code = 200
    r.text = text
    return r


def _ok_json_response(payload: dict) -> MagicMock:
    r = MagicMock()
    r.ok = True
    r.status_code = 200
    r.json.return_value = payload
    return r


def _err_response(status: int, body: str = "boom") -> MagicMock:
    r = MagicMock()
    r.ok = False
    r.status_code = status
    r.text = body
    return r


def test_token_minted_on_first_request():
    policies_xml = (
        "<policies><policy><id>1</id><name>Install Slack</name></policy></policies>"
    )
    with patch("jamf_client.requests.Session") as SessionMock:
        sess = SessionMock.return_value
        sess.post.return_value = _ok_token_response()
        sess.request.return_value = _ok_xml_response(policies_xml)

        client = JamfClient("foo.jamfcloud.com", "cid", "csec")
        out = client.list_policies()

        assert sess.post.call_count == 1, "token should be minted exactly once"
        token_url = sess.post.call_args[0][0]
        assert token_url == "https://foo.jamfcloud.com/api/oauth/token", \
            f"unexpected token url: {token_url}"
        assert sess.request.call_count == 1, "single resource GET expected"
        method, url = sess.request.call_args[0][:2]
        assert method == "GET" and url == "https://foo.jamfcloud.com/JSSResource/policies"
        headers = sess.request.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer tkn-abc", \
            f"bearer header missing: {headers}"
        assert out == [{"id": "1", "name": "Install Slack"}], f"got: {out!r}"


def test_token_refreshed_on_401():
    policies_xml = "<policies><policy><id>9</id><name>Reboot</name></policy></policies>"
    with patch("jamf_client.requests.Session") as SessionMock:
        sess = SessionMock.return_value
        # Two token mints: initial + post-401 refresh.
        sess.post.side_effect = [_ok_token_response(), _ok_token_response()]
        # First request 401s, second succeeds.
        sess.request.side_effect = [_err_response(401, "expired"), _ok_xml_response(policies_xml)]

        client = JamfClient("foo.jamfcloud.com", "cid", "csec")
        out = client.list_policies()

        assert sess.post.call_count == 2, "token should be re-minted on 401"
        assert sess.request.call_count == 2, "request must be retried once"
        assert out == [{"id": "9", "name": "Reboot"}]


def test_classic_api_xml_parsed():
    xml = (
        "<policies>"
        "<policy><id>1</id><name>Install Slack</name></policy>"
        "<policy><id>2</id><name>Update Chrome</name></policy>"
        "</policies>"
    )
    with patch("jamf_client.requests.Session") as SessionMock:
        sess = SessionMock.return_value
        sess.post.return_value = _ok_token_response()
        sess.request.return_value = _ok_xml_response(xml)

        client = JamfClient("foo.jamfcloud.com", "cid", "csec")
        out = client.list_policies()

        assert out == [
            {"id": "1", "name": "Install Slack"},
            {"id": "2", "name": "Update Chrome"},
        ], f"got: {out!r}"


def test_v2_api_json_parsed():
    payload = {
        "totalCount": 2,
        "results": [
            {"id": 1, "name": "All Macs", "is_smart": True},
            {"id": 2, "name": "Static Lab", "is_smart": False},
        ],
    }
    with patch("jamf_client.requests.Session") as SessionMock:
        sess = SessionMock.return_value
        sess.post.return_value = _ok_token_response()
        sess.request.return_value = _ok_json_response(payload)

        client = JamfClient("foo.jamfcloud.com", "cid", "csec")
        out = client.list_smart_groups()

        assert out == [
            {"id": "1", "name": "All Macs", "is_smart": True},
            {"id": "2", "name": "Static Lab", "is_smart": False},
        ], f"got: {out!r}"


def test_jamf_error_on_non_2xx():
    long_body = "x" * 500
    with patch("jamf_client.requests.Session") as SessionMock:
        sess = SessionMock.return_value
        sess.post.return_value = _ok_token_response()
        sess.request.return_value = _err_response(500, long_body)

        client = JamfClient("foo.jamfcloud.com", "cid", "csec")
        try:
            client.list_policies()
        except JamfError as e:
            msg = str(e)
            assert "500" in msg, f"status missing from error: {msg}"
            # Body must be truncated (200 char ceiling).
            body_part = msg.split(": ", 1)[-1]
            assert len(body_part) <= 200, f"body not truncated: {len(body_part)}"
        else:
            raise AssertionError("expected JamfError on 500 response")


def test_fqdn_strip_trailing_slash():
    with patch("jamf_client.requests.Session"):
        c1 = JamfClient("https://x.jamfcloud.com/", "cid", "csec")
        assert c1.base == "https://x.jamfcloud.com", f"got: {c1.base!r}"

        c2 = JamfClient("x.jamfcloud.com", "cid", "csec")
        assert c2.base == "https://x.jamfcloud.com", f"got: {c2.base!r}"

        c3 = JamfClient("http://x.jamfcloud.com///", "cid", "csec")
        assert c3.base == "https://x.jamfcloud.com", f"got: {c3.base!r}"


_TESTS = [
    test_token_minted_on_first_request,
    test_token_refreshed_on_401,
    test_classic_api_xml_parsed,
    test_v2_api_json_parsed,
    test_jamf_error_on_non_2xx,
    test_fqdn_strip_trailing_slash,
]


def main() -> int:
    passes = 0
    failures: list[tuple[str, str]] = []
    for fn in _TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
            passes += 1
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}: {e}")
            failures.append((fn.__name__, str(e)))
        except Exception as e:
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
            failures.append((fn.__name__, f"{type(e).__name__}: {e}"))

    print()
    print(f"{passes}/{len(_TESTS)} passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
