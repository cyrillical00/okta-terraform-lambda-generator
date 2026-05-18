from urllib.parse import urlparse

from okta_client import OktaClient, OktaError
from aws_client import AWSClient, AWSError
from gcp_client import GcpClient, GcpError
from jamf_client import JamfClient, JamfError


def _parse_org_url(url: str) -> tuple[str, str]:
    """Parse 'https://integrator-2720791.okta.com' -> ('integrator-2720791', 'okta.com').

    Handles okta.com, oktapreview.com, okta-emea.com, and any custom subdomain.
    Returns ('', '') if the URL cannot be parsed.
    """
    if not url:
        return ("", "")
    try:
        host = urlparse(url).netloc or url
        host = host.replace("https://", "").replace("http://", "").rstrip("/")
        parts = host.split(".", 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            return (parts[0], parts[1])
    except Exception:
        pass
    return ("", "")


def fetch_okta_context(org_url: str, api_token: str) -> dict:
    if not org_url or not api_token:
        return {"connected": False, "error": "Not configured — add OKTA_ORG_URL and OKTA_API_TOKEN to secrets."}
    try:
        client = OktaClient(org_url, api_token)
        return {
            "connected": True,
            "org_url": org_url,
            "groups": client.list_groups(),
            "apps": client.list_apps(),
            "event_hooks": client.list_event_hooks(),
            "error": None,
        }
    except OktaError as e:
        return {"connected": False, "error": str(e)}
    except Exception as e:
        return {"connected": False, "error": f"Unexpected error: {e}"}


def fetch_aws_context(region: str, access_key: str = "", secret_key: str = "") -> dict:
    if not region:
        return {"connected": False, "error": "Not configured — add AWS_REGION to secrets."}
    try:
        client = AWSClient(region, access_key, secret_key)
        return {
            "connected": True,
            "region": region,
            "lambda_functions": client.list_lambda_functions(),
            "iam_roles": client.list_iam_roles(),
            "error": None,
        }
    except AWSError as e:
        return {"connected": False, "error": str(e)}
    except Exception as e:
        return {"connected": False, "error": f"Unexpected error: {e}"}


def fetch_gcp_context(project_id: str, sa_json: str = "", region: str = "us-central1") -> dict:
    if not project_id:
        return {"connected": False, "error": "Not configured — add GCP_PROJECT_ID to secrets."}
    try:
        client = GcpClient(project_id, sa_json, region)
    except GcpError as e:
        return {"connected": False, "error": str(e)}
    except Exception as e:
        return {"connected": False, "error": f"Unexpected error: {e}"}

    # Per-service partial-success: a sandbox project without billing typically
    # has Cloud Run disabled while functions/IAM/pubsub work. Don't fail the
    # whole context just because one of four list calls hit SERVICE_DISABLED.
    partial_errors: list[str] = []

    def _safe(label: str, fn):
        try:
            return fn() or []
        except GcpError as exc:
            partial_errors.append(f"{label}: {exc}")
            return []

    result = {
        "connected": True,
        "project_id": project_id,
        "region": region,
        "functions": _safe("functions", client.list_functions),
        "service_accounts": _safe("service_accounts", client.list_service_accounts),
        "pubsub_topics": _safe("pubsub_topics", client.list_pubsub_topics),
        "run_services": _safe("run_services", client.list_run_services),
        "error": None,
        "partial_errors": partial_errors,
    }
    return result


def fetch_jamf_context(fqdn: str, client_id: str, client_secret: str) -> dict:
    if not fqdn or not client_id or not client_secret:
        return {"connected": False, "error": "Not configured, add JAMF_FQDN, JAMF_CLIENT_ID and JAMF_CLIENT_SECRET to secrets."}
    try:
        client = JamfClient(fqdn, client_id, client_secret)
    except JamfError as e:
        return {"connected": False, "error": str(e)}
    except Exception as e:
        return {"connected": False, "error": f"Unexpected error: {e}"}

    # Per-call partial success: the provider docs note that some endpoints can
    # be 403'd by tight role scopes (e.g. read-only API role missing scripts
    # permission). Don't fail the whole context just because one list call did.
    partial_errors: list[str] = []

    def _safe(label: str, fn):
        try:
            return fn() or []
        except JamfError as exc:
            partial_errors.append(f"{label}: {exc}")
            return []

    canonical_fqdn = fqdn.replace("https://", "").replace("http://", "").rstrip("/")
    return {
        "connected": True,
        "fqdn": canonical_fqdn,
        "is_cloud": canonical_fqdn.endswith(".jamfcloud.com"),
        "policies": _safe("policies", client.list_policies),
        "smart_groups": _safe("smart_groups", client.list_smart_groups),
        "scripts": _safe("scripts", client.list_scripts),
        "error": None,
        "partial_errors": partial_errors,
    }


def build_env_context(
    okta_org_url: str,
    okta_api_token: str,
    aws_region: str,
    aws_access_key: str = "",
    aws_secret_key: str = "",
    gcp_project_id: str = "",
    gcp_sa_json: str = "",
    gcp_region: str = "us-central1",
    jamf_fqdn: str = "",
    jamf_client_id: str = "",
    jamf_client_secret: str = "",
    fleet_url: str = "",
    fleet_api_token: str = "",
    snowflake_account: str = "",
    snowflake_user: str = "",
    snowflake_private_key: str = "",
    snowflake_role: str = "",
    snowflake_warehouse: str = "",
    snowflake_passphrase: str = "",
) -> dict:
    return {
        "okta": fetch_okta_context(okta_org_url, okta_api_token),
        "aws": fetch_aws_context(aws_region, aws_access_key, aws_secret_key),
        "gcp": fetch_gcp_context(gcp_project_id, gcp_sa_json, gcp_region or "us-central1"),
        "jamf": fetch_jamf_context(jamf_fqdn, jamf_client_id, jamf_client_secret),
        "fleet": fetch_fleet_context(fleet_url, fleet_api_token),
        "snowflake": fetch_snowflake_context(
            snowflake_account,
            snowflake_user,
            snowflake_private_key,
            snowflake_role,
            snowflake_warehouse,
            snowflake_passphrase or None,
        ),
    }


def fetch_fleet_context(url: str, api_token: str) -> dict:
    """Best-effort fetch of live Fleet labels / policies / queries / teams.

    Returns the canonical env-context shape used by render_env_pills and the
    sidebar status block: `{connected, url, labels, policies, queries, teams,
    error, partial_errors}`. When `url` or `api_token` is empty, short-circuits
    to the disconnected shape without contacting the network.

    Per-endpoint failures are non-fatal and append a one-line summary to
    `partial_errors`, matching the JAMF pattern.

    Secret name precedence (resolved by the caller in app.py:_load_env_context):
      url:       FLEET_URL  -> FLEETDM_URL
      api_token: FLEET_API_TOKEN -> FLEETDM_API_TOKEN
    The legacy `FLEET_*` names land first so Phase 14 deployments keep working
    without secret rotation; the `FLEETDM_*` names match the upstream provider
    (l-teles/fleetdm v0.5.4) docs and are the recommended choice for new users.
    """
    if not url or not api_token:
        return {"connected": False, "error": "Not configured, add FLEET_URL and FLEET_API_TOKEN (or FLEETDM_URL and FLEETDM_API_TOKEN) to secrets."}
    try:
        from fleet_client import FleetClient, FleetError
    except ImportError as e:
        return {"connected": False, "error": f"Fleet client unavailable: {e}"}
    try:
        client = FleetClient(url, api_token)
    except FleetError as e:
        return {"connected": False, "error": str(e)}
    except Exception as e:
        return {"connected": False, "error": f"Unexpected error: {e}"}

    partial_errors: list[str] = []

    def _safe(label: str, fn):
        try:
            return fn() or []
        except FleetError as exc:
            partial_errors.append(f"{label}: {exc}")
            return []

    canonical_url = url.rstrip("/")
    labels = _safe("labels", client.list_labels)
    policies = _safe("policies", client.list_policies)
    queries = _safe("queries", client.list_queries)
    teams = _safe("teams", client.list_teams)

    # Per-team policy fan-out. Best-effort: a single team's 404 or auth error
    # appends to partial_errors but does not abort. Mirrors the JAMF pattern.
    team_policies: dict[int, list[dict]] = {}
    for team in teams:
        team_id = team.get("id")
        if team_id is None:
            continue
        try:
            tp = client.list_team_policies(team_id) or []
            if tp:
                team_policies[team_id] = tp
        except FleetError as exc:
            team_name = team.get("name", f"id={team_id}")
            partial_errors.append(f"team_policies[{team_name}]: {exc}")

    return {
        "connected": True,
        "url": canonical_url,
        "labels": labels,
        "policies": policies,
        "team_policies": team_policies,
        "queries": queries,
        "teams": teams,
        "error": None,
        "partial_errors": partial_errors,
    }


def fetch_snowflake_context(
    account: str,
    user: str,
    private_key: str,
    role: str,
    warehouse: str,
    passphrase: str | None = None,
) -> dict:
    """Best-effort fetch of live Snowflake warehouses / databases / roles /
    users.

    Returns the canonical env-context shape used by render_env_pills and
    the sidebar status block: `{connected, account, warehouses, databases,
    roles, users, error, partial_errors}`. When any required credential is
    missing, short-circuits to the disconnected shape without contacting
    the network.

    Per-SHOW failures are non-fatal and append a one-line summary to
    `partial_errors`, matching the JAMF / Fleet pattern. A read-only
    service role typically cannot SHOW USERS; that single failure should
    not abort the whole context fetch.

    Secret expectations (resolved by the caller in app.py:_load_env_context):
      account:     SNOWFLAKE_ACCOUNT
      user:        SNOWFLAKE_USER
      private_key: SNOWFLAKE_PRIVATE_KEY  (PEM string, BEGIN/END markers included)
      role:        SNOWFLAKE_ROLE
      warehouse:   SNOWFLAKE_WAREHOUSE
      passphrase:  SNOWFLAKE_PRIVATE_KEY_PASSPHRASE  (optional, encrypted keys only)
    """
    if not account or not user or not private_key or not role or not warehouse:
        return {
            "connected": False,
            "error": (
                "Not configured, add SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, "
                "SNOWFLAKE_PRIVATE_KEY, SNOWFLAKE_ROLE, and SNOWFLAKE_WAREHOUSE "
                "to secrets."
            ),
        }
    try:
        from snowflake_client import SnowflakeClient, SnowflakeError
    except ImportError as e:
        return {"connected": False, "error": f"Snowflake client unavailable: {e}"}

    try:
        client = SnowflakeClient(
            account=account,
            user=user,
            private_key=private_key,
            role=role,
            warehouse=warehouse,
            passphrase=passphrase or None,
        )
    except SnowflakeError as e:
        return {"connected": False, "error": str(e)}
    except Exception as e:
        return {"connected": False, "error": f"Unexpected error: {e}"}

    partial_errors: list[str] = []

    def _safe(label: str, fn):
        try:
            return fn() or []
        except SnowflakeError as exc:
            partial_errors.append(f"{label}: {exc}")
            return []

    try:
        warehouses = _safe("warehouses", client.list_warehouses)
        databases = _safe("databases", client.list_databases)
        roles = _safe("roles", client.list_roles)
        users = _safe("users", client.list_users)
    finally:
        client.close()

    return {
        "connected": True,
        "account": account,
        "warehouses": warehouses,
        "databases": databases,
        "roles": roles,
        "users": users,
        "error": None,
        "partial_errors": partial_errors,
    }


def format_context_for_prompt(env_context: dict) -> str:
    """Returns a formatted string for injection into the generation prompt. Empty string if nothing connected."""
    okta = env_context.get("okta", {})
    aws = env_context.get("aws", {})
    gcp = env_context.get("gcp", {})
    jamf = env_context.get("jamf", {})
    fleet = env_context.get("fleet", {})
    snowflake = env_context.get("snowflake", {})
    sections = []

    if okta.get("connected"):
        lines = ["### Okta live resources"]
        org_url = okta.get("org_url", "")
        org_name, base_url = _parse_org_url(org_url)
        if org_name and base_url:
            lines.append("**Okta org metadata** (use these literal values in the provider block — see Live-environment override in SECTION B):")
            lines.append(f'  - org_name: "{org_name}"')
            lines.append(f'  - base_url: "{base_url}"')
        groups = okta.get("groups", [])
        if groups:
            lines.append("**Groups** (reference via data \"okta_group\"):")
            for g in groups[:60]:
                lines.append(f'  - name: "{g["name"]}"  id: {g["id"]}')
        apps = okta.get("apps", [])
        if apps:
            lines.append("**Apps** (reference via data \"okta_app_saml\" or \"okta_app_oauth\"):")
            for a in apps[:40]:
                lines.append(f'  - name: "{a["name"]}"  id: {a["id"]}  type: {a.get("sign_on_mode", "")}')
        hooks = okta.get("event_hooks", [])
        if hooks:
            lines.append("**Event hooks** (reference via data \"okta_event_hook\"):")
            for h in hooks:
                lines.append(f'  - name: "{h["name"]}"  id: {h["id"]}  status: {h.get("status", "")}')
        sections.append("\n".join(lines))

    if aws.get("connected"):
        lines = ["### AWS live resources"]
        fns = aws.get("lambda_functions", [])
        if fns:
            lines.append("**Lambda functions** (reference via data \"aws_lambda_function\"):")
            for fn in fns[:40]:
                lines.append(f'  - name: "{fn["name"]}"  arn: {fn["arn"]}')
        roles = aws.get("iam_roles", [])
        if roles:
            lines.append("**IAM roles** (reference via data \"aws_iam_role\"):")
            for r in roles[:40]:
                lines.append(f'  - name: "{r["name"]}"  arn: {r["arn"]}')
        sections.append("\n".join(lines))

    if gcp.get("connected"):
        lines = ["### GCP live resources"]
        project_id = gcp.get("project_id", "")
        region = gcp.get("region", "us-central1")
        if project_id:
            lines.append("**GCP project metadata** (use these literal values in the provider block):")
            lines.append(f'  - project: "{project_id}"')
            lines.append(f'  - region: "{region}"')
        fns = gcp.get("functions", [])
        if fns:
            lines.append("**Cloud Functions** (reference via data \"google_cloudfunctions2_function\"):")
            for fn in fns[:40]:
                lines.append(f'  - name: "{fn["name"]}"  uri: {fn.get("uri", "")}')
        sas = gcp.get("service_accounts", [])
        if sas:
            lines.append("**Service accounts** (reference via data \"google_service_account\"):")
            for sa in sas[:40]:
                lines.append(f'  - email: "{sa["email"]}"  display_name: "{sa.get("display_name", "")}"')
        topics = gcp.get("pubsub_topics", [])
        if topics:
            lines.append("**Pub/Sub topics** (reference via data \"google_pubsub_topic\"):")
            for t in topics[:40]:
                lines.append(f'  - name: "{t["name"]}"  full_name: {t["full_name"]}')
        run_svcs = gcp.get("run_services", [])
        if run_svcs:
            lines.append("**Cloud Run services** (reference via data \"google_cloud_run_v2_service\"):")
            for s in run_svcs[:40]:
                lines.append(f'  - name: "{s["name"]}"  uri: {s.get("uri", "")}')
        sections.append("\n".join(lines))

    if jamf.get("connected"):
        lines = ["### JAMF Pro live resources"]
        fqdn = jamf.get("fqdn", "")
        if fqdn:
            lines.append("**JAMF Pro instance metadata** (use these literal values in the provider block):")
            lines.append(f'  - jamfpro_instance_fqdn: "{fqdn}"')
            if jamf.get("is_cloud"):
                lines.append('  - jamfpro_load_balancer_lock: true   # JAMF Cloud requires this')
        policies = jamf.get("policies", [])
        if policies:
            lines.append('**Policies** (reference via data "jamfpro_policy"):')
            for p in policies[:40]:
                lines.append(f'  - name: "{p["name"]}"  id: {p["id"]}')
        smart_groups = jamf.get("smart_groups", [])
        if smart_groups:
            lines.append('**Smart groups** (reference via data "jamfpro_smart_computer_group_v2"):')
            for g in smart_groups[:40]:
                lines.append(f'  - name: "{g["name"]}"  id: {g["id"]}')
        scripts = jamf.get("scripts", [])
        if scripts:
            lines.append('**Scripts** (reference via data "jamfpro_script"):')
            for s in scripts[:40]:
                lines.append(f'  - name: "{s["name"]}"  id: {s["id"]}')
        sections.append("\n".join(lines))

    if fleet.get("connected"):
        lines = ["### Fleet MDM live resources"]
        url = fleet.get("url", "")
        if url:
            lines.append("**Fleet instance metadata** (use this URL in the provider block):")
            lines.append(f'  - fleet_url: "{url}"')
        labels = fleet.get("labels", [])
        if labels:
            lines.append('**Labels** (reference by name in YAML labels_include_any, or by id in Terraform):')
            for l in labels[:100]:
                lines.append(f'  - name: "{l.get("name", "")}"  id: {l.get("id", "")}')
            if len(labels) > 100:
                lines.append(f'  ({len(labels)} total, showing 100)')
        policies = fleet.get("policies", [])
        if policies:
            lines.append('**Policies** (global; existing policy names, do not duplicate):')
            for p in policies[:100]:
                lines.append(f'  - name: "{p.get("name", "")}"  id: {p.get("id", "")}')
            if len(policies) > 100:
                lines.append(f'  ({len(policies)} total, showing 100)')
        queries = fleet.get("queries", [])
        if queries:
            lines.append('**Queries** (existing saved query names; do not duplicate):')
            for q in queries[:100]:
                lines.append(f'  - name: "{q.get("name", "")}"  id: {q.get("id", "")}')
            if len(queries) > 100:
                lines.append(f'  ({len(queries)} total, showing 100)')
        teams = fleet.get("teams", [])
        if teams:
            lines.append('**Teams** (use team_id when scoping a fleetdm_fleet resource):')
            for t in teams[:100]:
                lines.append(f'  - name: "{t.get("name", "")}"  id: {t.get("id", "")}')
            if len(teams) > 100:
                lines.append(f'  ({len(teams)} total, showing 100)')
        team_policies = fleet.get("team_policies", {}) or {}
        if team_policies:
            # Build a lookup so we can render the team's display name alongside its id.
            team_name_by_id = {t.get("id"): t.get("name", "") for t in teams}
            for team_id, tp_list in team_policies.items():
                if not tp_list:
                    continue
                team_name = team_name_by_id.get(team_id, "")
                lines.append(f'**Team "{team_name}" (id={team_id}) policies** (do not duplicate):')
                for p in tp_list[:100]:
                    lines.append(f'  - name: "{p.get("name", "")}"  id: {p.get("id", "")}')
                if len(tp_list) > 100:
                    lines.append(f'  ({len(tp_list)} total, showing 100)')
        sections.append("\n".join(lines))

    if snowflake.get("connected"):
        lines = ["### Snowflake live resources"]
        account = snowflake.get("account", "")
        if account:
            lines.append("**Snowflake account metadata** (use this account identifier in the provider block):")
            lines.append(f'  - account: "{account}"')
        warehouses = snowflake.get("warehouses", [])
        if warehouses:
            lines.append('**Warehouses** (reference by name in snowflake_warehouse or as default_warehouse):')
            for w in warehouses[:50]:
                lines.append(f'  - name: "{w.get("name", "")}"  size: {w.get("size", "")}  state: {w.get("state", "")}')
            if len(warehouses) > 50:
                lines.append(f'  ({len(warehouses)} total, showing 50)')
        databases = snowflake.get("databases", [])
        if databases:
            lines.append('**Databases** (existing database names; do not duplicate):')
            for d in databases[:50]:
                lines.append(f'  - name: "{d.get("name", "")}"  owner: {d.get("owner", "")}')
            if len(databases) > 50:
                lines.append(f'  ({len(databases)} total, showing 50)')
        roles = snowflake.get("roles", [])
        if roles:
            lines.append('**Roles** (existing role names; do not duplicate, reference via snowflake_account_role):')
            for r in roles[:50]:
                lines.append(f'  - name: "{r.get("name", "")}"  owner: {r.get("owner", "")}')
            if len(roles) > 50:
                lines.append(f'  ({len(roles)} total, showing 50)')
        users = snowflake.get("users", [])
        if users:
            lines.append('**Users** (existing usernames; do not duplicate, reference via snowflake_user):')
            for u in users[:50]:
                lines.append(f'  - name: "{u.get("name", "")}"  default_role: {u.get("default_role", "")}')
            if len(users) > 50:
                lines.append(f'  ({len(users)} total, showing 50)')
        sections.append("\n".join(lines))

    if not sections:
        return ""

    header = (
        "\n\n## Live environment context\n\n"
        "The following resources already exist in the connected environment. "
        "They are listed by name and id below.\n\n"
        "### Decision rule: `data` vs `resource` (apply BEFORE writing any HCL)\n\n"
        "For every group, app, or hook mentioned in the prompt, run this check:\n\n"
        "  STEP 1. Look at the lists below. Does the exact name appear there "
        "(case-sensitive, whitespace-sensitive)?\n"
        "  STEP 2a. YES, the name appears: emit a `data` block. Add a comment of the form "
        "`# Resolved from live environment, id: <REAL_ID_COPIED_FROM_LIST_BELOW>` using the actual id "
        "from the list. Never invent an id.\n"
        "  STEP 2b. NO, the name does NOT appear: emit a `resource` block to CREATE the entity. "
        "Do not emit a `data` block. Do not invent an id. Do not write a "
        "'Resolved from live environment' comment.\n\n"
        "### FORBIDDEN behaviors (these are credibility-destroying hallucinations)\n\n"
        "  - Emitting `data \"okta_group\" \"x\" { name = \"X\" }` when \"X\" is not in the Groups "
        "list below. (Plan will fail with `group with name \"X\" does not exist`.)\n"
        "  - Fabricating a `# Resolved from live environment, id: 00g...` comment with an id you "
        "did not literally read off the list below.\n"
        "  - Inventing plausible-looking Okta ids (groups start with `00g`, apps with `0oa`, "
        "hooks with `who`). Every id in your output must be either copied verbatim from the lists "
        "below or be a Terraform reference like `okta_group.foo.id`.\n\n"
        "When in doubt, choose `resource` (create) over `data` (lookup). A surplus group is "
        "harmless; a hallucinated data source crashes `terraform apply`.\n\n"
        "### Example (group IS in the list)\n\n"
        "  # Resolved from live environment, id: 00g1abc2defGhIjkl3m4\n"
        "  data \"okta_group\" \"engineering\" {\n"
        "    name = \"Engineering\"\n"
        "  }\n\n"
        "### Counter-example (group is NOT in the list, so emit resource)\n\n"
        "  resource \"okta_group\" \"hr\" {\n"
        "    name        = \"HR\"\n"
        "    description = \"HR department\"\n"
        "  }\n\n"
    )
    return header + "\n\n".join(sections)
