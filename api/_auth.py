"""X-API-Key / Bearer verification for the HTTP API (Phase 21a).

Phase 21a introduced per-key API tokens in roles.toml. Each entry maps a
SHA-256 hex of the issued token to an actor_id, role, and daily quota in
USD. Incoming requests are matched in constant time via
`hmac.compare_digest` against the hashed table.

Backwards compatibility: `TFGEN_API_KEY` (the legacy single shared
secret) is still accepted. When matched, the caller is tagged with the
synthetic actor_id `legacy-tfgen` so audit + cost still get a stable
identifier, and the daily quota falls back to `TFGEN_HTTP_DAILY_QUOTA_USD`
or the `[cost] daily_cap_usd` entry in roles.toml.

Headers accepted (either works):
  - `X-API-Key: <token>` (preferred; matches the pre-Phase-21 surface)
  - `Authorization: Bearer <token>` (matches the spec language)
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import Header, HTTPException

# Python 3.11+ ships tomllib in stdlib. Older interpreters would need the
# `tomli` backport; the repo targets 3.11+ already so the stdlib import
# is correct.
try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover, older runtimes only
    import tomli as tomllib  # type: ignore[no-redef]


_LEGACY_ACTOR_ID = "legacy-tfgen"
_DEFAULT_LEGACY_QUOTA = 5.00


@dataclass(frozen=True)
class ApiKeyEntry:
    """One row from roles.toml [api_keys.*]."""
    actor_id: str
    role: str
    daily_quota_usd: float
    sha256: str


def _roles_toml_path() -> Path:
    """Resolve the path to roles.toml at the repo root.

    Override via the `TFGEN_ROLES_TOML` env var (used by tests so each
    test case can point at a temp file without colliding with the
    repo's real config).
    """
    override = (os.environ.get("TFGEN_ROLES_TOML") or "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "roles.toml"


def _load_api_keys() -> dict[str, ApiKeyEntry]:
    """Return a map of sha256-hex -> ApiKeyEntry. Empty on any error.

    Reads roles.toml on every request. The file is tiny (one entry per
    issued token) so the read cost is negligible compared to the LLM
    work that follows; reading fresh means a rotation lands immediately
    without a Vercel cold-start.
    """
    path = _roles_toml_path()
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    raw = data.get("api_keys") or {}
    out: dict[str, ApiKeyEntry] = {}
    for _key_name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        sha = (entry.get("sha256") or "").strip().lower()
        actor = (entry.get("actor_id") or "").strip()
        role = (entry.get("role") or "viewer").strip()
        try:
            quota = float(entry.get("daily_quota_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            quota = 0.0
        if not sha or not actor:
            continue
        out[sha] = ApiKeyEntry(
            actor_id=actor,
            role=role,
            daily_quota_usd=max(0.0, quota),
            sha256=sha,
        )
    return out


def _legacy_key() -> str:
    return (os.environ.get("TFGEN_API_KEY") or "").strip()


def _legacy_quota() -> float:
    """Quota for the legacy TFGEN_API_KEY actor.

    Reads from `TFGEN_HTTP_DAILY_QUOTA_USD` first (preserves the
    pre-Phase-21 contract), then falls back to `[cost] daily_cap_usd`
    in roles.toml, then to a hard default.
    """
    raw = (os.environ.get("TFGEN_HTTP_DAILY_QUOTA_USD") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    path = _roles_toml_path()
    if path.exists():
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
            cost_tbl = data.get("cost") or {}
            return max(0.0, float(cost_tbl.get("daily_cap_usd", _DEFAULT_LEGACY_QUOTA)))
        except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
            pass
    return _DEFAULT_LEGACY_QUOTA


def actor_id_for(api_key: str) -> str:
    """Legacy helper: stable 16-char hash of a raw token.

    Kept for backwards compat with callers (and tests) that still derive
    an actor_id from the raw key shape. Phase 21+ callers should prefer
    `resolve_token(...)` which returns the operator-assigned actor_id
    from roles.toml.
    """
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:16]


def _sha256_hex(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def resolve_token(token: str) -> Optional[ApiKeyEntry]:
    """Look up a bearer token against the [api_keys] table.

    Returns the matching ApiKeyEntry on success, or None when no entry
    matches (including the legacy TFGEN_API_KEY path; that's resolved
    separately by verify_api_key).

    Comparison is done in constant time per row via
    `hmac.compare_digest` on the SHA-256 hex. Empty / malformed tokens
    short-circuit to None.
    """
    if not token:
        return None
    digest = _sha256_hex(token)
    for sha_hex, entry in _load_api_keys().items():
        if hmac.compare_digest(digest, sha_hex):
            return entry
    return None


def _parse_bearer(authorization: Optional[str]) -> str:
    """Extract the token from an `Authorization: Bearer <token>` header.

    Tolerant: returns "" when the header is missing, the scheme is not
    Bearer, or the token is empty after stripping.
    """
    if not authorization:
        return ""
    s = authorization.strip()
    if not s.lower().startswith("bearer "):
        return ""
    return s[7:].strip()


async def verify_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> str:
    """FastAPI dependency. Returns the caller's actor_id on success;
    raises 401 on missing / wrong key.

    Resolution order:
      1. [api_keys] table in roles.toml (per-key tokens, Phase 21a)
      2. TFGEN_API_KEY env var (legacy single shared secret)
      3. 401 otherwise.

    Constant-time compare via `hmac.compare_digest` everywhere so a
    timing oracle cannot enumerate a token one byte at a time.

    No 503 path: a misconfigured server (no TFGEN_API_KEY and an empty
    [api_keys] table) returns 401 like any other unauthenticated
    request. Telling the caller the server is misconfigured leaks
    operational state without giving them anything actionable; 401 is
    the safer default once per-key tokens exist.
    """
    provided = (x_api_key or "").strip() or _parse_bearer(authorization)
    if not provided:
        raise HTTPException(status_code=401, detail="missing API key")

    # Path 1: per-key token from roles.toml [api_keys].
    entry = resolve_token(provided)
    if entry is not None:
        return entry.actor_id

    # Path 2: legacy TFGEN_API_KEY (backwards compat).
    legacy = _legacy_key()
    if legacy and hmac.compare_digest(provided, legacy):
        return _LEGACY_ACTOR_ID

    raise HTTPException(status_code=401, detail="invalid API key")


def quota_for_actor(actor_id: str) -> float:
    """Look up the daily quota in USD for a known actor_id.

    For per-key actors, returns the `daily_quota_usd` from roles.toml.
    For the legacy actor (`legacy-tfgen`), returns the env-var /
    [cost] daily_cap_usd fallback.
    Returns 0.0 when no entry matches (cost.py treats 0 as unlimited
    for Streamlit RBAC purposes; the headless caller is gated by the
    `TFGEN_HTTP_DAILY_QUOTA_USD` env var separately so unknown actors
    still pay the env default).
    """
    if actor_id == _LEGACY_ACTOR_ID:
        return _legacy_quota()
    for entry in _load_api_keys().values():
        if entry.actor_id == actor_id:
            return entry.daily_quota_usd
    return 0.0
