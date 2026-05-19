"""Kandji (Iru) REST client for live env-context fetching.

Pinned API endpoints (Kandji / Iru REST API, https://api-docs.kandji.io/):

  Blueprints:        GET /api/v1/blueprints                 (paginated; offset/limit)
  Library items:     GET /api/v1/library/library-items      (paginated)
  Tags:              GET /api/v1/tags                       (paginated)

Auth is a tenant-level bearer token: `Authorization: Bearer <KANDJI_API_TOKEN>`.
Tokens are minted from Settings -> Access -> Add API Token in the Kandji web
console. Read-only scopes are sufficient for env-context fetching; do NOT use
a token with write scopes here.

Base URL pattern: `https://<subdomain>.api.kandji.io` (US region) or
`https://<subdomain>.clients.eu.kandji.io` (EU region). The caller passes the
already-assembled base URL.

Pagination uses Kandji's documented offset/limit scheme. The API caps each
response at 300 items by default; we use 300 as the page size and walk the
offset until a short page is returned, with a hard cap of 100 pages
(30,000 items) per endpoint to prevent runaway loops on API regressions.

NOTE: Kandji rebranded to Iru in late 2025 and the Terraform provider source
moved to MScottBlake/iru. The REST API endpoints kept the `kandji.io` host
and `/api/v1/...` paths for backwards compatibility as of the schema dump
date (2026-05-18).
"""

from __future__ import annotations

import requests


class KandjiError(Exception):
    """Base class for Kandji client errors. env_context.fetch_kandji_context
    catches this base type to record partial failures without aborting the
    whole context fetch."""
    pass


class KandjiAuthError(KandjiError):
    """401 / 403 from Kandji. Most often a bad or revoked API token, or a
    role-restricted token that cannot read the requested resource."""
    pass


class KandjiServerError(KandjiError):
    """5xx, network failure, or timeout. Transient by nature; retry might
    succeed. Distinguished from KandjiAuthError so the UI can surface the
    right tooltip wording."""
    pass


class KandjiParseError(KandjiError):
    """Response decoded but the JSON shape did not match what the Kandji API
    docs document. Surface this so future API drift is visible."""
    pass


class KandjiNotFoundError(KandjiError):
    """404 from Kandji. Callers typically downgrade this to an empty list."""
    pass


class KandjiClient:
    """Bearer-auth REST client. One instance per env-context fetch; does not
    cache responses across instances (callers cache via st.session_state)."""

    DEFAULT_TIMEOUT = (10, 30)  # (connect, read) seconds, matches jamf/fleet_client
    PAGE_SIZE = 300             # Kandji default upper bound per docs
    MAX_PAGES = 100             # safety cap, 100 * 300 = 30k items

    def __init__(self, base_url: str, api_token: str):
        if not base_url:
            raise KandjiError("Kandji base_url is required.")
        if not api_token:
            raise KandjiError("Kandji api_token is required.")
        self.base = base_url.rstrip("/")
        self.api_token = api_token
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
            raise KandjiServerError(f"Kandji request timed out: {e}") from e
        except requests.RequestException as e:
            raise KandjiServerError(f"Kandji request failed: {e}") from e

        if resp.status_code in (401, 403):
            raise KandjiAuthError(
                f"Kandji API rejected the token ({resp.status_code}); check KANDJI_API_TOKEN role."
            )
        if resp.status_code == 404:
            raise KandjiNotFoundError(f"Kandji API returned 404 for {path}.")
        if resp.status_code == 429:
            # Kandji caps at 10,000 req/hr per customer. Surface the
            # Retry-After header if present so the caller sees a useful hint.
            retry_after = ""
            try:
                ra = resp.headers.get("Retry-After")
                if ra:
                    retry_after = f" Retry-After: {ra}s."
            except Exception:
                retry_after = ""
            raise KandjiServerError(
                f"Kandji API rate-limited (429).{retry_after}"
            )
        if resp.status_code >= 500:
            raise KandjiServerError(
                f"Kandji server error {resp.status_code}: {resp.text[:200]}"
            )
        if not resp.ok:
            raise KandjiServerError(
                f"Kandji API error {resp.status_code}: {resp.text[:200]}"
            )

        try:
            return resp.json()
        except ValueError as e:
            raise KandjiParseError(
                f"Kandji response is not JSON: {resp.text[:200]}"
            ) from e

    @staticmethod
    def _extract_list(payload, *keys: str) -> list[dict]:
        """Kandji list endpoints return either a bare JSON array or a wrapped
        object with a `results` / `data` / endpoint-named key. Try the
        wrapper keys first, fall back to the bare list shape."""
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            raise KandjiParseError(
                f"Kandji response is not a JSON object or array: {type(payload).__name__}"
            )
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        # Permissive fallback: future API shape change should not abort the
        # whole env-context fetch.
        return []

    def _request_paginated(self, path: str, *item_keys: str, per_page: int = PAGE_SIZE) -> list[dict]:
        """Walk Kandji's offset/limit pagination (offset=0, offset=300, ...)
        until a short or empty page is returned. Concatenates the per-page
        lists and returns the full result.

        Termination conditions:
          - page returns fewer than `per_page` items (last page)
          - page returns an empty list
          - safety cap of MAX_PAGES (100) reached: raises KandjiServerError

        The `?` vs `&` separator is chosen automatically based on whether
        `path` already contains a query string.
        """
        items: list[dict] = []
        for page in range(self.MAX_PAGES):
            offset = page * per_page
            sep = "&" if "?" in path else "?"
            page_path = f"{path}{sep}offset={offset}&limit={per_page}"
            payload = self._request(page_path)
            page_items = self._extract_list(payload, *item_keys)
            items.extend(page_items)
            if len(page_items) < per_page:
                return items
        raise KandjiServerError(
            f"Kandji pagination exceeded safety cap ({self.MAX_PAGES} pages, "
            f"{self.MAX_PAGES * per_page} items) on {path}."
        )

    def list_blueprints(self) -> list[dict]:
        # Documented under https://api-docs.kandji.io/#... Blueprints group.
        # Common wrapper keys seen in the wild: "results", "blueprints".
        return self._request_paginated(
            "/api/v1/blueprints", "results", "blueprints"
        )

    def list_library_items(self) -> list[dict]:
        # Library items collection. Wrapper key historically "results".
        return self._request_paginated(
            "/api/v1/library/library-items", "results", "library_items"
        )

    def list_tags(self) -> list[dict]:
        # Tags collection. Wrapper key historically "results".
        return self._request_paginated(
            "/api/v1/tags", "results", "tags"
        )
