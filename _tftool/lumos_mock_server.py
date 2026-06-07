"""Local Lumos API mock server for end-to-end POC verification without a
real Lumos tenant.

Lumos is enterprise-only with no public sandbox, so this stdlib `http.server`
shim fakes just enough of the REST surface to let the env-context fetcher in
`env_context.fetch_lumos_context` flip the 8th pill green and stream realistic
apps / groups / requestable-permissions counts into the prompt context.

## What it serves

  GET /apps                       -> 3 apps + next_page_token; page 2 returns 2 more
  GET /groups                     -> 4 groups (single page, no next token)
  GET /requestable_permissions    -> 5 permissions (single page)
  GET anything else               -> 404

Pagination is the canonical Lumos `next_page_token` cursor shape: first call
returns `next_page_token: "page-2"`, the second call (`?page_token=page-2`)
returns no token. This exercises the LumosClient._request_paginated loop end
to end, including the multi-page concatenation path.

## What it does NOT do

- Auth-check the bearer token beyond requiring a `lsk_` prefix (any token
  matching that shape is accepted; the goal is local smoke, not security)
- Validate request bodies (the env-context layer only does GETs)
- Mutate state (no POST / PATCH / DELETE)
- Match the full Lumos OpenAPI schema (fields are minimal; only what the
  env-context formatter renders)

## How to run

  cd "C:/Users/cbot/TF Tool"
  python _tftool/lumos_mock_server.py
  # Listens on http://127.0.0.1:8765 by default
  # Override with: python _tftool/lumos_mock_server.py 9000

Then in `.streamlit/secrets.toml`:

  LUMOS_ACCESS_TOKEN = "lsk_LOCAL_MOCK_TOKEN_FOR_DEV_ONLY"
  LUMOS_SERVER_URL   = "http://127.0.0.1:8765"

Reboot the Streamlit app (signature changes; see
[[streamlit-cloud-hot-reload-caches-modules]]) and the Lumos pill should
flip to `Lumos (12)` (3+2 apps + 4 groups + 5 permissions = 14 once page 2
of /apps lands).
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs


_FAKE_APPS_PAGE_1 = [
    {
        "id": "00000000-0000-0000-0000-000000000001",
        "name": "Slack",
        "instance_id": "slack-prod",
        "category": "Collaboration",
        "description": "Workspace chat for the whole company.",
        "status": "active",
    },
    {
        "id": "00000000-0000-0000-0000-000000000002",
        "name": "Notion",
        "instance_id": "notion-prod",
        "category": "Productivity",
        "description": "Internal wiki and docs.",
        "status": "active",
    },
    {
        "id": "00000000-0000-0000-0000-000000000003",
        "name": "GitHub Enterprise",
        "instance_id": "github-ent",
        "category": "Engineering",
        "description": "Source control for engineering org.",
        "status": "active",
    },
]
_FAKE_APPS_PAGE_2 = [
    {
        "id": "00000000-0000-0000-0000-000000000004",
        "name": "Datadog",
        "instance_id": "datadog-prod",
        "category": "Engineering",
        "description": "APM + infra monitoring.",
        "status": "active",
    },
    {
        "id": "00000000-0000-0000-0000-000000000005",
        "name": "Zendesk",
        "instance_id": "zendesk-prod",
        "category": "Customer Support",
        "description": "Customer ticketing.",
        "status": "active",
    },
]

_FAKE_GROUPS = [
    {"id": "g-eng",     "name": "Engineering",       "app_id": "github-ent"},
    {"id": "g-sales",   "name": "Sales",             "app_id": "salesforce-prod"},
    {"id": "g-support", "name": "Customer Support",  "app_id": "zendesk-prod"},
    {"id": "g-execs",   "name": "Executives",        "app_id": "okta-prod"},
]

_FAKE_PERMISSIONS = [
    {"id": "p-slack-admin",   "label": "Slack workspace admin", "app_id": "00000000-0000-0000-0000-000000000001"},
    {"id": "p-slack-member",  "label": "Slack member",          "app_id": "00000000-0000-0000-0000-000000000001"},
    {"id": "p-notion-editor", "label": "Notion editor",         "app_id": "00000000-0000-0000-0000-000000000002"},
    {"id": "p-gh-admin",      "label": "GitHub org admin",      "app_id": "00000000-0000-0000-0000-000000000003"},
    {"id": "p-gh-write",      "label": "GitHub write",          "app_id": "00000000-0000-0000-0000-000000000003"},
]


class _Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return auth.startswith("Bearer lsk_") or auth.startswith("Bearer lsk-")

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming convention)
        if not self._auth_ok():
            return self._json(401, {"error": "missing or malformed bearer token; expected `Bearer lsk_*`"})

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path == "/apps":
            token = (query.get("page_token") or [""])[0]
            if token == "page-2":
                return self._json(200, {"results": _FAKE_APPS_PAGE_2})
            return self._json(200, {"results": _FAKE_APPS_PAGE_1, "next_page_token": "page-2"})

        if path == "/groups":
            return self._json(200, {"results": _FAKE_GROUPS})

        if path == "/requestable_permissions":
            return self._json(200, {"results": _FAKE_PERMISSIONS})

        return self._json(404, {"error": f"no mock handler for {path}"})

    def log_message(self, fmt: str, *args) -> None:  # noqa: N802
        # Print to stderr in a quieter format than the default.
        sys.stderr.write(f"[lumos-mock] {self.address_string()} - {fmt % args}\n")


def main() -> None:
    port = 8765
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            sys.stderr.write(f"port must be an integer, got: {sys.argv[1]}\n")
            sys.exit(2)
    server = HTTPServer(("127.0.0.1", port), _Handler)
    print(f"Lumos mock server listening on http://127.0.0.1:{port}")
    print("  GET /apps                     -> 3 + 2 apps over 2 pages (next_page_token cursor)")
    print("  GET /groups                   -> 4 groups")
    print("  GET /requestable_permissions  -> 5 permissions")
    print("Auth: Bearer lsk_* (any token matching the prefix is accepted).")
    print("Set LUMOS_ACCESS_TOKEN=lsk_LOCAL_MOCK_TOKEN_FOR_DEV_ONLY and")
    print(f"    LUMOS_SERVER_URL=http://127.0.0.1:{port} in .streamlit/secrets.toml.")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nLumos mock server stopping.")
        server.server_close()


if __name__ == "__main__":
    main()
