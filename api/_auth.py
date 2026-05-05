"""X-API-Key verification for the HTTP API.

Single shared secret in v1: clients send the key in `X-API-Key`; we
compare against `os.environ['TFGEN_API_KEY']` in constant time and return
a stable `actor_id` (16-char SHA256 prefix of the key) for use as the
identifier passed to `audit.log`, `cost.wrap_client`, and `cost.today_usd`.

Per-API-key quotas and a `[api_keys]` table are explicitly out of scope
(see `humming-dazzling-sunbeam.md`). When that lands later, this module
gains a lookup step before the constant-time compare; everything
downstream stays the same.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from fastapi import Header, HTTPException


def _expected_key() -> str:
    return (os.environ.get("TFGEN_API_KEY") or "").strip()


def actor_id_for(api_key: str) -> str:
    """Stable 16-char hash for use as the identifier for audit + cost.
    Mirrors the hashing audit.py / cost.py do internally — passing the
    raw key as the `email` param works, but hashing here lets the
    response surface a non-secret correlation id without exposing the
    key itself.
    """
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:16]


async def verify_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> str:
    """FastAPI dependency. Returns the caller's actor_id on success;
    raises 401 on missing / wrong key.

    Constant-time compare via `hmac.compare_digest` so a timing oracle
    can't enumerate the secret one byte at a time.
    """
    expected = _expected_key()
    if not expected:
        # Misconfiguration on the server side — surface as 503 so callers
        # don't think their key is wrong when the server simply has no
        # secret loaded.
        raise HTTPException(status_code=503, detail="server is not configured: TFGEN_API_KEY missing")

    provided = (x_api_key or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")

    return actor_id_for(provided)
