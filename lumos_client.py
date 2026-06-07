"""Lumos REST client for live env-context fetching.

Pinned API endpoints (Lumos REST API, https://developers.lumos.com/reference):

  Apps:                      GET /apps                       (paginated; next_page_token)
  Groups:                    GET /groups                     (paginated; next_page_token)
  Requestable permissions:   GET /requestable_permissions    (paginated; next_page_token)

Auth is a Personal Access Token (PAT) used as an HTTP bearer:
`Authorization: Bearer lsk_<token>`. Tokens are minted in the Lumos web console
under Settings -> Developers -> Personal Access Tokens. Read-only scopes are
sufficient for env-context fetching; do NOT use a token with write scopes here.

Base URL is fixed at `https://api.lumos.com`. Unlike Kandji / Fleet / JAMF
there is no per-tenant subdomain; tenants are identified by the bearer token.

Pagination uses Lumos's documented `next_page_token` cursor scheme. Each
response includes an optional `next_page_token` string at the top level; when
present, the next request passes it as `?page_token=<token>`. Termination is
on empty response, missing `next_page_token`, or the safety cap (100 pages).
This differs from Kandji's offset/limit; Lumos's tokens are opaque blobs
returned by the server and MUST NOT be reconstructed client-side.

NOTE: Resource attribute names in SECTION M are grounded against the
`teamlumos/lumos` v0.10.3 provider binary (schema dump at
`_tftool/lumos_schema.json`, captured 2026-06-01). The provider auth attribute
is `http_bearer`, NOT `access_token` (an aliasing some older blog posts and
LLM-memorized examples use). See SECTION M's BINARY SCHEMA REALITY CHECK
preamble for the full divergence list.
"""

from __future__ import annotations

import requests


class LumosError(Exception):
    """Base class for Lumos client errors. env_context.fetch_lumos_context
    catches this base type to record partial failures without aborting the
    whole context fetch."""
    pass


class LumosAuthError(LumosError):
    """401 / 403 from Lumos. Most often a bad or revoked PAT, or a
    role-restricted token that cannot read the requested resource."""
    pass


class LumosServerError(LumosError):
    """5xx, network failure, timeout, or 429 rate-limit. Transient by nature;
    retry might succeed. Distinguished from LumosAuthError so the UI can
    surface the right tooltip wording."""
    pass


class LumosParseError(LumosError):
    """Response decoded but the JSON shape did not match what the Lumos API
    docs document. Surface this so future API drift is visible."""
    pass


class LumosNotFoundError(LumosError):
    """404 from Lumos. Callers typically downgrade this to an empty list."""
    pass


class LumosClient:
    """Bearer-auth REST client. One instance per env-context fetch; does not
    cache responses across instances (callers cache via st.session_state)."""

    DEFAULT_TIMEOUT = (10, 30)  # (connect, read) seconds, matches kandji/fleet
    DEFAULT_BASE_URL = "https://api.lumos.com"
    MAX_PAGES = 100             # safety cap on token-walked pagination

    def __init__(self, api_token: str, base_url: str = DEFAULT_BASE_URL):
        if not api_token:
            raise LumosError("Lumos api_token is required.")
        self.api_token = api_token
        self.base = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.session = requests.Session()

    def _request(self, path: str) -> object:
        full_url = f"{self.base}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
        }
        try:
            resp = self.session.get(full_url, headers=headers, timeout=self.DEFAULT_TIMEOUT)
        except requests.Timeout as e:
            raise LumosServerError(f"Lumos request timed out: {e}") from e
        except requests.RequestException as e:
            raise LumosServerError(f"Lumos request failed: {e}") from e

        if resp.status_code in (401, 403):
            raise LumosAuthError(
                f"Lumos API rejected the token ({resp.status_code}); check LUMOS_ACCESS_TOKEN scope."
            )
        if resp.status_code == 404:
            raise LumosNotFoundError(f"Lumos API returned 404 for {path}.")
        if resp.status_code == 429:
            retry_after = ""
            try:
                ra = resp.headers.get("Retry-After")
                if ra:
                    retry_after = f" Retry-After: {ra}s."
            except Exception:
                retry_after = ""
            raise LumosServerError(
                f"Lumos API rate-limited (429).{retry_after}"
            )
        if resp.status_code >= 500:
            raise LumosServerError(
                f"Lumos server error {resp.status_code}: {resp.text[:200]}"
            )
        if not resp.ok:
            raise LumosServerError(
                f"Lumos API error {resp.status_code}: {resp.text[:200]}"
            )

        try:
            return resp.json()
        except ValueError as e:
            raise LumosParseError(
                f"Lumos response is not JSON: {resp.text[:200]}"
            ) from e

    @staticmethod
    def _extract_list(payload, *keys: str) -> list[dict]:
        """Lumos list endpoints return either a bare array, a wrapped object
        with a `results` / `data` / endpoint-named key, or an OpenAPI-style
        object with `items`. Try the wrapper keys first, fall back to bare."""
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            raise LumosParseError(
                f"Lumos response is not a JSON object or array: {type(payload).__name__}"
            )
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        # Permissive fallback: future API shape change should not abort the
        # whole env-context fetch.
        return []

    def _request_paginated(self, path: str, *item_keys: str) -> list[dict]:
        """Walk Lumos's `next_page_token` cursor pagination until a page
        without a token is returned. Concatenates per-page lists.

        Termination conditions:
          - response lacks `next_page_token` (or it is empty / null)
          - response yields an empty list AND no token
          - safety cap of MAX_PAGES (100) reached: raises LumosServerError

        Token is opaque; we pass it through unchanged as `page_token`.
        """
        items: list[dict] = []
        next_token: str | None = None
        for _ in range(self.MAX_PAGES):
            if next_token:
                sep = "&" if "?" in path else "?"
                page_path = f"{path}{sep}page_token={next_token}"
            else:
                page_path = path
            payload = self._request(page_path)
            page_items = self._extract_list(payload, *item_keys)
            items.extend(page_items)
            if isinstance(payload, dict):
                next_token = payload.get("next_page_token") or None
            else:
                next_token = None
            if not next_token:
                return items
        raise LumosServerError(
            f"Lumos pagination exceeded safety cap ({self.MAX_PAGES} pages) on {path}."
        )

    def list_apps(self) -> list[dict]:
        # Documented under https://developers.lumos.com/reference/list_apps
        # Common wrapper keys observed: "results", "apps", "items".
        return self._request_paginated(
            "/apps", "results", "apps", "items"
        )

    def list_groups(self) -> list[dict]:
        # Documented under the groups collection.
        return self._request_paginated(
            "/groups", "results", "groups", "items"
        )

    def list_requestable_permissions(self) -> list[dict]:
        # Documented under https://developers.lumos.com/reference/list_requestable_permissions
        return self._request_paginated(
            "/requestable_permissions",
            "results", "requestable_permissions", "permissions", "items",
        )
