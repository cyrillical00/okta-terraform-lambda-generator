from urllib.parse import urlparse

from okta_client import OktaClient, OktaError
from aws_client import AWSClient, AWSError


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


def build_env_context(
    okta_org_url: str,
    okta_api_token: str,
    aws_region: str,
    aws_access_key: str = "",
    aws_secret_key: str = "",
) -> dict:
    return {
        "okta": fetch_okta_context(okta_org_url, okta_api_token),
        "aws": fetch_aws_context(aws_region, aws_access_key, aws_secret_key),
    }


def format_context_for_prompt(env_context: dict) -> str:
    """Returns a formatted string for injection into the generation prompt. Empty string if nothing connected."""
    okta = env_context.get("okta", {})
    aws = env_context.get("aws", {})
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
