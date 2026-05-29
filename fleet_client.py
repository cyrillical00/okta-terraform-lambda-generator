"""Fleet MDM REST client for live env-context fetching.

Pinned API endpoints (Fleet server >= 4.82.0):

  Labels:        GET /api/v1/fleet/labels                    (JSON, paginated)
  Policies:      GET /api/v1/fleet/global/policies           (JSON, global only, paginated)
  Team policies: GET /api/v1/fleet/teams/<id>/policies       (JSON, paginated; 404 on Fleet Free)
  Queries:       GET /api/v1/fleet/queries                   (JSON, paginated)
  Teams:         GET /api/v1/fleet/teams                     (JSON, paginated; empty on Fleet Free)

Auth is bearer-token: `Authorization: Bearer <FLEET_API_TOKEN>`. The token is
obtained from Fleet UI -> Account -> API token. Read-only token roles
(Observer / Maintainer) are sufficient for env-context fetching; do NOT use
a GitOps Admin token here.

Pagination uses Fleet's documented `?page=N&per_page=100` scheme (page is
zero-indexed). The client walks pages until a short page is returned, with a
hard cap of 100 pages (10,000 items) per endpoint to prevent runaway loops on
API regressions.
"""

from __future__ import annotations

import requests


class FleetError(Exception):
    """Base class for Fleet client errors. fleet_validate.fetch_fleet_context
    catches this base type to record partial failures without aborting the
    whole context fetch."""
    pass


class FleetAuthError(FleetError):
    """401 / 403 from Fleet. Most often a bad or revoked API token, or a
    role-restricted token that cannot read the requested resource."""
    pass


class FleetServerError(FleetError):
    """5xx, network failure, or timeout. Transient by nature; retry might
    succeed. Distinguished from FleetAuthError so the UI can surface the
    right tooltip wording."""
    pass


class FleetParseError(FleetError):
    """Response decoded but the JSON shape did not match what the Fleet API
    docs document. Surface this so future API drift is visible."""
    pass


class FleetNotFoundError(FleetError):
    """404 from Fleet. Resource (e.g. a per-team policy listing on Fleet Free)
    does not exist. Callers typically downgrade this to an empty list."""
    pass


class FleetClient:
    """Bearer-auth REST client. One instance per env-context fetch; does not
    cache responses across instances (callers cache via st.session_state)."""

    DEFAULT_TIMEOUT = (10, 30)  # (connect, read) seconds, matches jamf_client
    PAGE_SIZE = 100             # Fleet docs default; keep in lockstep
    MAX_PAGES = 100             # hard safety cap, 100 pages * 100 items = 10k

    def __init__(self, url: str, api_token: str):
        if not url:
            raise FleetError("Fleet url is required.")
        if not api_token:
            raise FleetError("Fleet api_token is required.")
        self.base = url.rstrip("/")
        self.api_token = api_token
        self.session = requests.Session()

    def _request(self, path: str) -> dict:
        full_url = f"{self.base}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
        }
        try:
            resp = self.session.get(full_url, headers=headers, timeout=self.DEFAULT_TIMEOUT)
        except requests.Timeout as e:
            raise FleetServerError(f"Fleet request timed out: {e}") from e
        except requests.RequestException as e:
            raise FleetServerError(f"Fleet request failed: {e}") from e

        if resp.status_code in (401, 403):
            raise FleetAuthError(f"Fleet API rejected the token ({resp.status_code}); check FLEET_API_TOKEN role.")
        if resp.status_code == 404:
            raise FleetNotFoundError(f"Fleet API returned 404 for {path}.")
        if resp.status_code == 429:
            # Fleet sets Retry-After on rate-limit responses; surface it so the
            # caller sees a useful hint instead of a generic 429.
            retry_after = ""
            try:
                ra = resp.headers.get("Retry-After")
                if ra:
                    retry_after = f" Retry-After: {ra}s."
            except Exception:
                retry_after = ""
            raise FleetServerError(f"Fleet API rate-limited (429).{retry_after}")
        if resp.status_code >= 500:
            raise FleetServerError(f"Fleet server error {resp.status_code}: {resp.text[:200]}")
        if not resp.ok:
            raise FleetServerError(f"Fleet API error {resp.status_code}: {resp.text[:200]}")

        try:
            return resp.json()
        except ValueError as e:
            raise FleetParseError(f"Fleet response is not JSON: {resp.text[:200]}") from e

    @staticmethod
    def _extract_list(payload: dict, *keys: str) -> list[dict]:
        """Fleet wraps list responses inconsistently across endpoints. Some
        return `{"labels": [...]}`, others `{"policies": [...]}`, etc. Walk
        the candidate keys and return the first list found; surface an empty
        list if none match (preserves the partial-error pattern)."""
        if not isinstance(payload, dict):
            raise FleetParseError(f"Fleet response is not a JSON object: {type(payload).__name__}")
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        # Permissive fallback: the API might wrap under another key in a
        # future version. Empty list is the conservative read.
        return []

    def _request_paginated(self, path: str, item_key: str, per_page: int = PAGE_SIZE) -> list[dict]:
        """Walk Fleet's page-based pagination (page=0, page=1, ...) until a
        short or empty page is returned. Concatenates the per-page lists and
        returns the full result.

        Termination conditions:
          - page returns fewer than `per_page` items (last page)
          - page returns an empty list
          - safety cap of MAX_PAGES (100) reached: raises FleetServerError

        The `?` vs `&` separator is chosen automatically based on whether
        `path` already contains a query string.
        """
        items: list[dict] = []
        for page in range(self.MAX_PAGES):
            sep = "&" if "?" in path else "?"
            page_path = f"{path}{sep}page={page}&per_page={per_page}"
            payload = self._request(page_path)
            page_items = self._extract_list(payload, item_key)
            items.extend(page_items)
            if len(page_items) < per_page:
                return items
        raise FleetServerError(
            f"Fleet pagination exceeded safety cap ({self.MAX_PAGES} pages, "
            f"{self.MAX_PAGES * per_page} items) on {path}."
        )

    def list_labels(self) -> list[dict]:
        return self._request_paginated("/api/v1/fleet/labels", "labels")

    def list_policies(self) -> list[dict]:
        # Global policies live at /global/policies. Fleet removed the bare
        # /api/v1/fleet/policies route by 4.85 (it 404s), so pin the documented
        # global path here. Per-team policies use /teams/<id>/policies below.
        return self._request_paginated("/api/v1/fleet/global/policies", "policies")

    def list_queries(self) -> list[dict]:
        return self._request_paginated("/api/v1/fleet/queries", "queries")

    def list_teams(self) -> list[dict]:
        return self._request_paginated("/api/v1/fleet/teams", "teams")

    def list_team_policies(self, team_id: int) -> list[dict]:
        """Per-team policies. On Fleet Free the teams concept does not exist
        and this endpoint returns 404; surface that as an empty list rather
        than an exception so a single missing team does not abort the whole
        env-context fetch."""
        try:
            return self._request_paginated(
                f"/api/v1/fleet/teams/{team_id}/policies", "policies"
            )
        except FleetNotFoundError:
            return []
