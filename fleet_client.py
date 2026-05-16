"""Fleet MDM REST client for live env-context fetching.

Pinned API endpoints (Fleet server >= 4.82.0):

  Labels:    GET /api/v1/fleet/labels             (JSON)
  Policies:  GET /api/v1/fleet/policies           (JSON, global only)
  Queries:   GET /api/v1/fleet/queries            (JSON)
  Teams:     GET /api/v1/fleet/teams              (JSON; empty list on Fleet Free)

Auth is bearer-token: `Authorization: Bearer <FLEET_API_TOKEN>`. The token is
obtained from Fleet UI -> Account -> API token. Read-only token roles
(Observer / Maintainer) are sufficient for env-context fetching; do NOT use
a GitOps Admin token here.

Per-team policies (`/api/v1/fleet/teams/<id>/policies`) and pagination are
both deferred; the first cut covers the global slice that almost every Fleet
deployment exposes.
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


class FleetClient:
    """Bearer-auth REST client. One instance per env-context fetch — does not
    cache responses across instances (callers cache via st.session_state)."""

    DEFAULT_TIMEOUT = (10, 30)  # (connect, read) seconds, matches jamf_client

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

    def list_labels(self) -> list[dict]:
        payload = self._request("/api/v1/fleet/labels")
        return self._extract_list(payload, "labels")

    def list_policies(self) -> list[dict]:
        payload = self._request("/api/v1/fleet/policies")
        return self._extract_list(payload, "policies")

    def list_queries(self) -> list[dict]:
        payload = self._request("/api/v1/fleet/queries")
        return self._extract_list(payload, "queries")

    def list_teams(self) -> list[dict]:
        payload = self._request("/api/v1/fleet/teams")
        return self._extract_list(payload, "teams")
