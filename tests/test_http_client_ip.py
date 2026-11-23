"""The rate-limiter key must not be something the caller can choose."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("TFGEN_API_KEY", "test-secret-do-not-ship")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-fake")

from fastapi.testclient import TestClient

from api import index
from headless_rate_limit import HTTP_RATE_LIMITER


def _request(headers: dict, peer: str | None = "192.0.2.10"):
    lowered = {k.lower(): v for k, v in headers.items()}
    return SimpleNamespace(
        headers=SimpleNamespace(get=lambda name, default=None: lowered.get(name.lower(), default)),
        client=SimpleNamespace(host=peer) if peer else None,
    )


def test_platform_header_wins_over_forwarded_for():
    req = _request({"X-Real-IP": "203.0.113.7", "X-Forwarded-For": "6.6.6.6, 203.0.113.7"})
    assert index._client_ip(req) == "203.0.113.7"
    assert index._client_ip(_request({"X-Vercel-Forwarded-For": "203.0.113.8"})) == "203.0.113.8"


def test_left_most_forwarded_entry_is_never_trusted():
    # the caller wrote "6.6.6.6"; the proxy appended the address it saw
    assert index._client_ip(_request({"X-Forwarded-For": "6.6.6.6, 198.51.100.4"})) == "198.51.100.4"
    assert index._client_ip(_request({"X-Forwarded-For": " 198.51.100.4 "})) == "198.51.100.4"


def test_fallbacks():
    assert index._client_ip(_request({})) == "192.0.2.10"
    assert index._client_ip(_request({"X-Forwarded-For": " , "})) == "192.0.2.10"
    assert index._client_ip(_request({}, peer=None)) == "unknown"


def test_rotating_a_spoofed_forwarded_for_does_not_reset_the_limit():
    """30 requests per minute per client, whatever the caller claims to be."""
    HTTP_RATE_LIMITER.reset()
    try:
        with patch("api.index.push_to_github", return_value="https://github.com/o/r/commit/a"), \
             patch("api.index.audit.log"), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fake_for_test",
                                     "TFGEN_PUSH_ALLOWED_REPOS": "owner/repo"}):
            client = TestClient(index.app)
            codes = []
            for i in range(33):
                r = client.post(
                    "/api/push",
                    headers={
                        "X-API-Key": os.environ["TFGEN_API_KEY"],
                        # what the platform reports
                        "X-Real-IP": "203.0.113.50",
                        # what the caller makes up, different every time
                        "X-Forwarded-For": f"10.9.8.{i}",
                    },
                    json={"files": {"terraform/a.tf": "x"}, "repo": "owner/repo",
                          "branch": "feature/x", "commit_message": "m"},
                )
                codes.append(r.status_code)
        assert codes[:30] == [200] * 30
        assert codes[30:] == [429, 429, 429]
    finally:
        HTTP_RATE_LIMITER.reset()
