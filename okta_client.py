import requests


class OktaError(Exception):
    pass


class OktaClient:
    def __init__(self, org_url: str, api_token: str):
        self.base = org_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"SSWS {api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def _paginated(self, url: str) -> list:
        results = []
        while url:
            resp = self.session.get(url, timeout=10)
            if not resp.ok:
                raise OktaError(f"Okta API error {resp.status_code}: {resp.text[:200]}")
            results.extend(resp.json())
            url = None
            for part in resp.headers.get("Link", "").split(","):
                part = part.strip()
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
                    break
        return results

    def list_groups(self, limit: int = 200) -> list[dict]:
        items = self._paginated(f"{self.base}/api/v1/groups?limit={limit}")
        return [{"id": g["id"], "name": g["profile"]["name"]} for g in items]

    def list_apps(self, limit: int = 200) -> list[dict]:
        items = self._paginated(f"{self.base}/api/v1/apps?limit={limit}")
        return [
            {
                "id": a["id"],
                "name": a["label"],
                "sign_on_mode": a.get("signOnMode", ""),
            }
            for a in items
        ]

    def list_event_hooks(self) -> list[dict]:
        resp = self.session.get(f"{self.base}/api/v1/eventHooks", timeout=10)
        if not resp.ok:
            raise OktaError(f"Okta API error {resp.status_code}: {resp.text[:200]}")
        return [
            {"id": h["id"], "name": h["name"], "status": h.get("status", "")}
            for h in resp.json()
        ]

    def list_users(self, limit: int = 100) -> list[dict]:
        items = self._paginated(f"{self.base}/api/v1/users?limit={limit}")
        return [
            {
                "id": u["id"],
                "login": u["profile"].get("login", ""),
                "email": u["profile"].get("email", ""),
                "first_name": u["profile"].get("firstName", ""),
                "last_name": u["profile"].get("lastName", ""),
            }
            for u in items
        ]

    def get_group_by_name(self, name: str) -> dict | None:
        resp = self.session.get(
            f"{self.base}/api/v1/groups",
            params={"q": name, "limit": 10},
            timeout=10,
        )
        if not resp.ok:
            raise OktaError(f"Okta API error {resp.status_code}: {resp.text[:200]}")
        for g in resp.json():
            if g["profile"]["name"].lower() == name.lower():
                return {"id": g["id"], "name": g["profile"]["name"]}
        return None

    def get_app_by_name(self, name: str) -> dict | None:
        resp = self.session.get(
            f"{self.base}/api/v1/apps",
            params={"q": name, "limit": 10},
            timeout=10,
        )
        if not resp.ok:
            raise OktaError(f"Okta API error {resp.status_code}: {resp.text[:200]}")
        for a in resp.json():
            if a["label"].lower() == name.lower():
                return {"id": a["id"], "name": a["label"], "sign_on_mode": a.get("signOnMode", "")}
        return None

    def get_user_by_login(self, login: str) -> dict | None:
        resp = self.session.get(
            f"{self.base}/api/v1/users/{requests.utils.quote(login, safe='')}",
            timeout=10,
        )
        if resp.status_code == 404:
            return None
        if not resp.ok:
            raise OktaError(f"Okta API error {resp.status_code}: {resp.text[:200]}")
        u = resp.json()
        return {
            "id": u["id"],
            "login": u["profile"].get("login", ""),
            "email": u["profile"].get("email", ""),
        }

    def get_resource_by_id(self, resource_type: str, uid: str) -> dict | None:
        """Fetch any resource by its Okta UID. resource_type: groups | apps | users | eventHooks"""
        resp = self.session.get(
            f"{self.base}/api/v1/{resource_type}/{uid}",
            timeout=10,
        )
        if resp.status_code == 404:
            return None
        if not resp.ok:
            raise OktaError(f"Okta API error {resp.status_code}: {resp.text[:200]}")
        return resp.json()
