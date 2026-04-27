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
        "When the intent references any resource listed here by name, "
        "generate a Terraform `data` source to look it up rather than using var.* for its ID. "
        "Add a comment above each data source with the actual ID or ARN for reference.\n\n"
        "STRICT RULE (do not hallucinate data sources): only emit a `data \"okta_group\"`, "
        "`data \"okta_app_saml\"`, `data \"okta_app_oauth\"`, or `data \"okta_event_hook\"` lookup "
        "for a name that appears verbatim in the lists below. If the prompt mentions a group, app, "
        "or hook by a name that is NOT in this list, emit a `resource` block to CREATE it instead. "
        "Hallucinating a data source for a name that does not exist in the live environment will "
        "fail `terraform apply` with a 'not found' error.\n\n"
        "Example:\n"
        "  # Resolved from live environment, id: 00g1abc2defGhIjkl3m4\n"
        "  data \"okta_group\" \"engineering\" {\n"
        "    name = \"Engineering\"\n"
        "  }\n\n"
    )
    return header + "\n\n".join(sections)
