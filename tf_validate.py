"""Shared helpers for running `terraform init` + `terraform validate` against
generated HCL outputs.

Used by:
  - _tftool/validate/run_validate.py (standalone CLI sweep over cache)
  - qa_runner.py (--terraform-validate post-pass guardrail)

The validate workspace lives under _tftool/validate/<TID>/ and the provider
plugin cache under _tftool/.terraform-plugin-cache so providers download once
across the whole sweep. Both paths are inside the gitignored _tftool/ tree.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = REPO_ROOT / "_tftool" / "validate"
PLUGIN_CACHE = REPO_ROOT / "_tftool" / ".terraform-plugin-cache"

VAR_DEFAULTS: dict[str, str] = {
    "gcp_project_id":           '"tf-tool-demo"',
    "gcp_region":               '"us-central1"',
    "container_image":          '"us-docker.pkg.dev/cloudrun/container/hello"',
    "container_port":           "8080",
    "max_instances":            "10",
    "min_instances":            "0",
    "service_account_id":       '"tf-validate-handler"',
    "service_cpu":              '"1"',
    "service_memory":           '"512Mi"',
    "service_name":             '"internal-api"',
    "service_timeout_seconds":  "60",
    "function_name":            '"handler"',
    "topic_name":               '"demo-events"',
    "schedule_expression":      '"0 9 * * *"',
    "okta_org_name":            '"trial-7898123"',
    "okta_base_url":            '"oktapreview.com"',
    "okta_api_token":           '"placeholder-token-for-validate-only"',
    "event_hook_url":           '"https://example.com/hook"',
    "event_hook_auth_token":    '"placeholder"',
    "webhook_endpoint":         '"https://example.com/hook"',
    "aws_region":               '"us-west-2"',
    "lambda_function_name":     '"handler"',
    # JAMF Pro (Cloud) placeholders. Validate sweep does not auth, so any
    # syntactically valid value works. The FQDN must look like a Cloud
    # tenant (jamfcloud.com) so the load_balancer_lock contract reads as
    # semantically correct in generated HCL.
    "jamfpro_instance_fqdn":    '"validate.jamfcloud.com"',
    "jamfpro_client_id":        '"placeholder-client-id-for-validate-only"',
    "jamfpro_client_secret":    '"placeholder-client-secret-for-validate-only"',
    "jamfpro_auth_method":      '"oauth2"',
    # Fleet TF (l-teles/fleetdm 0.5.4 experimental provider). Validate sweep
    # does not auth against a real Fleet server, so any syntactically valid
    # URL + token works for `terraform init && terraform validate`. Variable
    # names mirror the provider's actual attribute names (`server_address`,
    # `api_key`); the legacy `fleet_url` / `fleet_api_token` entries below
    # preserve backwards compatibility with any older HCL still in flight.
    "fleetdm_url":              '"https://fleet.invalid.example"',
    "fleetdm_api_key":          '"FAKE-API-KEY-FOR-VALIDATE-ONLY"',
    "fleet_url":                '"https://fleet.invalid.example"',
    "fleet_api_token":          '"FAKE-API-KEY-FOR-VALIDATE-ONLY"',
    # Snowflake (snowflakedb/snowflake ~> 2.0). Provider validates the auth
    # shape but does not connect during `terraform validate`; placeholders
    # that look syntactically valid let the validate sweep succeed.
    "snowflake_account":            '"placeholder.us-east-1"',
    "snowflake_user":               '"PLACEHOLDER_USER"',
    "snowflake_role":               '"SYSADMIN"',
    "snowflake_warehouse":          '"PLACEHOLDER_WH"',
    "snowflake_private_key":        '"-----BEGIN PRIVATE KEY-----\\nplaceholder\\n-----END PRIVATE KEY-----"',
    "snowflake_private_key_passphrase": '"placeholder"',
}


def parse_var_types(hcl: str) -> dict[str, str]:
    """Walk variable blocks and return {name: type_keyword}. Falls back to
    'string' when no `type =` line is present."""
    out: dict[str, str] = {}
    for m in re.finditer(r'variable\s+"([^"]+)"\s*\{', hcl):
        name = m.group(1)
        depth = 1
        i = m.end()
        while i < len(hcl) and depth > 0:
            if hcl[i] == "{":
                depth += 1
            elif hcl[i] == "}":
                depth -= 1
            i += 1
        body = hcl[m.end(): i - 1]
        tm = re.search(r"\btype\s*=\s*(\w+)", body)
        out[name] = tm.group(1) if tm else "string"
    return out


def default_for(var_name: str, var_type: str) -> str:
    if var_name in VAR_DEFAULTS:
        return VAR_DEFAULTS[var_name]
    if var_type == "number":
        return "0"
    if var_type == "bool":
        return "false"
    if var_type == "list":
        return "[]"
    if var_type == "map":
        return "{}"
    return '"placeholder"'


def write_workspace(tid: str, outputs: dict) -> Path:
    """Materialise <WORKSPACE_ROOT>/<TID>/ with okta.tf / lambda.tf / gcp.tf
    plus a terraform.tfvars built from variable types. Wipes stale .tf files
    before writing so a previous mode does not bleed into this run."""
    from generator.hcl_utils import dedupe_variable_blocks

    workdir = WORKSPACE_ROOT / tid
    workdir.mkdir(parents=True, exist_ok=True)
    for stale in workdir.glob("*.tf"):
        stale.unlink()
    # Wipe .terraform/ and .terraform.lock.hcl from prior runs. A stale lockfile
    # pinning an older provider version is the most common cause of the
    # "Terraform encountered problems during initialisation" flake when the
    # provider pin changes between runs (e.g. okta v3 -> v4 transition).
    stale_dir = workdir / ".terraform"
    if stale_dir.exists():
        shutil.rmtree(stale_dir, ignore_errors=True)
    stale_lock = workdir / ".terraform.lock.hcl"
    if stale_lock.exists():
        stale_lock.unlink()
    okta = outputs.get("terraform_okta_hcl", "") or ""
    lam = outputs.get("terraform_lambda_hcl", "") or ""
    gcp = outputs.get("terraform_gcp_hcl", "") or ""
    jamf = outputs.get("terraform_jamf_hcl", "") or ""
    fleet = outputs.get("terraform_fleet_hcl", "") or ""
    snowflake = outputs.get("terraform_snowflake_hcl", "") or ""
    if okta.strip() and lam.strip():
        okta, lam = dedupe_variable_blocks(okta, lam)
    if okta.strip() and gcp.strip():
        okta, gcp = dedupe_variable_blocks(okta, gcp)
    if okta.strip() and jamf.strip():
        okta, jamf = dedupe_variable_blocks(okta, jamf)
    if okta.strip() and fleet.strip():
        okta, fleet = dedupe_variable_blocks(okta, fleet)
    if okta.strip() and snowflake.strip():
        okta, snowflake = dedupe_variable_blocks(okta, snowflake)
    if okta.strip():
        (workdir / "okta.tf").write_text(okta, encoding="utf-8", newline="\n")
    if lam.strip():
        (workdir / "lambda.tf").write_text(lam, encoding="utf-8", newline="\n")
    if gcp.strip():
        (workdir / "gcp.tf").write_text(gcp, encoding="utf-8", newline="\n")
    if fleet.strip():
        (workdir / "fleet.tf").write_text(fleet, encoding="utf-8", newline="\n")
    if snowflake.strip():
        (workdir / "snowflake.tf").write_text(snowflake, encoding="utf-8", newline="\n")
    if jamf.strip():
        (workdir / "jamf.tf").write_text(jamf, encoding="utf-8", newline="\n")
    types = parse_var_types(okta + "\n" + lam + "\n" + gcp + "\n" + jamf + "\n" + fleet + "\n" + snowflake)
    if types:
        lines = [f"{name} = {default_for(name, t)}" for name, t in sorted(types.items())]
        (workdir / "terraform.tfvars").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    # filebase64sha256("path") and file("path") are both evaluated at
    # validate time. Touch any referenced relative paths so validate does
    # not fail on missing files. file() is what JAMF scripts and macOS
    # configuration profiles use to load script contents from disk.
    full_hcl = okta + "\n" + lam + "\n" + gcp + "\n" + jamf + "\n" + fleet + "\n" + snowflake
    for m in re.finditer(r'filebase64sha256\("([^"]+)"\)', full_hcl):
        rel_path = m.group(1)
        target = (workdir / rel_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(b"")
    for m in re.finditer(r'\bfile\("([^"]+)"\)', full_hcl):
        rel_path = m.group(1)
        target = (workdir / rel_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            # Non-empty stub so anything downstream that reads the contents
            # (length checks, hash diffs) sees a deterministic placeholder.
            target.write_bytes(b"#!/bin/bash\n# placeholder for terraform validate\n")
    return workdir


def run_terraform(workdir: Path, env: dict) -> tuple[bool, str]:
    """Run `terraform init -backend=false` then `terraform validate`. Return
    (ok, message). On failure, message is the first error line trimmed to 200
    chars. On success, message is the canonical 'configuration is valid' line.

    Init runs with one automatic retry after a 2s pause to absorb transient
    network blips and provider-registry timeouts. The retry was observed to
    close COMP08 / EM01 init flakes that surface ~10% of the time on a fresh
    plugin cache."""
    def _run_init():
        return subprocess.run(
            ["terraform", "init", "-backend=false", "-no-color", "-input=false"],
            cwd=workdir, env=env, capture_output=True, text=True, timeout=180,
        )

    init = _run_init()
    if init.returncode != 0:
        time.sleep(2)
        init = _run_init()
    if init.returncode != 0:
        excerpt = (init.stderr or init.stdout).strip().split("\n")
        first_err = next((l for l in excerpt if "Error" in l or "error" in l), excerpt[0] if excerpt else "init failed")
        return False, f"init: {first_err[:160]}"
    val = subprocess.run(
        ["terraform", "validate", "-no-color"],
        cwd=workdir, env=env, capture_output=True, text=True, timeout=60,
    )
    if val.returncode == 0:
        return True, "Success! The configuration is valid."
    excerpt = (val.stderr or val.stdout).strip().split("\n")
    first_err = next((l for l in excerpt if "Error" in l), excerpt[0] if excerpt else "validate failed")
    return False, first_err[:200]


def make_env() -> dict:
    """Return a process env preconfigured with TF_PLUGIN_CACHE_DIR and
    TF_INPUT=0. Creates the plugin cache directory if missing."""
    PLUGIN_CACHE.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["TF_PLUGIN_CACHE_DIR"] = str(PLUGIN_CACHE)
    env["TF_INPUT"] = "0"
    return env
