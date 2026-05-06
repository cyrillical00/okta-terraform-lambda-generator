"""JAMF Pro client for live env-context fetching.

Pinned API endpoints (as of provider deploymenttheory/jamfpro v0.37.0):

  Auth:                POST /api/oauth/token            (OAuth2 client_credentials)
  Policies:            GET  /JSSResource/policies       (Classic, returns XML)
  Smart groups:        GET  /api/v2/computer-groups     (JSON)
  Scripts:             GET  /api/v1/scripts             (JSON, paginated)
  Packages:            GET  /api/v1/packages            (JSON, paginated)
  Extension attributes GET  /JSSResource/computerextensionattributes (Classic, XML)

If JAMF deprecates any of the above, update both the path and the parser
shape (XML vs JSON) here. The Classic API has been stable for years, but
JAMF has been migrating individual endpoints to /api/v2 over time.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET

import requests


class JamfError(Exception):
    pass


class JamfClient:
    def __init__(self, fqdn: str, client_id: str, client_secret: str):
        if not fqdn:
            raise JamfError("JAMF fqdn is required.")
        if not client_id or not client_secret:
            raise JamfError("JAMF client_id and client_secret are required.")
        # Strip protocol + trailing slash; store canonical https base.
        host = fqdn.replace("https://", "").replace("http://", "").rstrip("/")
        self.base = f"https://{host}"
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = requests.Session()
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def _mint_token(self) -> str:
        url = f"{self.base}/api/oauth/token"
        body = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            resp = self.session.post(url, data=body, headers=headers, timeout=10)
        except requests.RequestException as e:
            raise JamfError(f"JAMF token request failed: {e}") from e
        if not resp.ok:
            raise JamfError(f"JAMF token error {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise JamfError(f"JAMF token response is not JSON: {resp.text[:200]}") from e
        token = payload.get("access_token")
        if not token:
            raise JamfError(f"JAMF token response missing access_token: {payload}")
        # 60s safety buffer before expiry.
        expires_in = int(payload.get("expires_in", 1800))
        self._token = token
        self._token_expires_at = time.time() + max(60, expires_in - 60)
        return token

    def _ensure_token(self) -> str:
        if not self._token or time.time() >= self._token_expires_at:
            return self._mint_token()
        return self._token

    def _request(self, method: str, path: str, *, accept: str = "application/json", **kw):
        token = self._ensure_token()
        headers = kw.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = accept
        url = f"{self.base}{path}"
        try:
            resp = self.session.request(method, url, headers=headers, timeout=10, **kw)
        except requests.RequestException as e:
            raise JamfError(f"JAMF request failed ({method} {path}): {e}") from e
        # Auto-refresh on 401: token may have been revoked early.
        if resp.status_code == 401:
            self._token = None
            token = self._mint_token()
            headers["Authorization"] = f"Bearer {token}"
            try:
                resp = self.session.request(method, url, headers=headers, timeout=10, **kw)
            except requests.RequestException as e:
                raise JamfError(f"JAMF retry failed ({method} {path}): {e}") from e
        if not resp.ok:
            raise JamfError(f"JAMF API error {resp.status_code}: {resp.text[:200]}")
        return resp

    def list_policies(self) -> list[dict]:
        # Classic endpoint, XML response: <policies><policy><id>1</id><name>X</name></policy>...</policies>
        resp = self._request("GET", "/JSSResource/policies", accept="application/xml")
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as e:
            raise JamfError(f"JAMF policies XML parse failed: {e}") from e
        result = []
        for p in root.findall("policy"):
            pid = p.findtext("id", default="").strip()
            name = p.findtext("name", default="").strip()
            if pid and name:
                result.append({"id": pid, "name": name})
        return result

    def list_smart_groups(self) -> list[dict]:
        # API v2 returns paged JSON; for computer-groups the typical shape has
        # totalCount + results. We loop through pages defensively.
        return self._paged_v2(
            "/api/v2/computer-groups",
            mapper=lambda g: {
                "id": str(g.get("id", "")),
                "name": g.get("name", ""),
                "is_smart": bool(g.get("is_smart", False)),
            },
        )

    def list_scripts(self) -> list[dict]:
        return self._paged_v2(
            "/api/v1/scripts",
            mapper=lambda s: {"id": str(s.get("id", "")), "name": s.get("name", "")},
        )

    def list_packages(self) -> list[dict]:
        return self._paged_v2(
            "/api/v1/packages",
            mapper=lambda p: {"id": str(p.get("id", "")), "name": p.get("name", "")},
        )

    def list_extension_attributes(self) -> list[dict]:
        # Classic XML endpoint chosen: stable across provider versions and
        # avoids the heavier /api/v1/computer-inventory route.
        # Shape: <computer_extension_attributes><computer_extension_attribute>
        #          <id>1</id><name>X</name>
        #        </computer_extension_attribute>...</computer_extension_attributes>
        resp = self._request(
            "GET",
            "/JSSResource/computerextensionattributes",
            accept="application/xml",
        )
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as e:
            raise JamfError(f"JAMF extension attrs XML parse failed: {e}") from e
        result = []
        for ea in root.findall("computer_extension_attribute"):
            eid = ea.findtext("id", default="").strip()
            name = ea.findtext("name", default="").strip()
            if eid and name:
                result.append({"id": eid, "name": name})
        return result

    def _paged_v2(self, path: str, *, mapper, page_size: int = 100) -> list[dict]:
        """Page through a JAMF v1/v2 list endpoint that returns
        {"totalCount": N, "results": [...]} until exhausted."""
        results: list[dict] = []
        page = 0
        while True:
            sep = "&" if "?" in path else "?"
            page_path = f"{path}{sep}page={page}&page-size={page_size}"
            resp = self._request("GET", page_path, accept="application/json")
            try:
                payload = resp.json()
            except ValueError as e:
                raise JamfError(f"JAMF JSON parse failed for {path}: {e}") from e
            batch = payload.get("results", []) or []
            if not batch:
                break
            results.extend(mapper(item) for item in batch)
            total = int(payload.get("totalCount", len(results)))
            if len(results) >= total:
                break
            page += 1
        return results
