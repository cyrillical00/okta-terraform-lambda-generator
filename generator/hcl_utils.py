"""Small HCL string utilities used by the generation pipeline.

Exposes `strip_provider_boilerplate`, which removes `terraform {}`,
`provider "okta|aws|google" {}`, and the corresponding shared variable
declarations from a generated HCL string. Used when pushing per-prompt
files (basename != "okta") so multiple generated `.tf` files can coexist in
the same Terraform module without "Duplicate required providers
configuration" / "Duplicate provider configuration" / "Duplicate variable
declaration" errors at `terraform init`.

When basename is empty (default single-file behavior), no stripping happens;
the file is the canonical `terraform/okta.tf` and must contain the boilerplate.
"""

from __future__ import annotations

import re

_BOILERPLATE_PATTERNS = [
    re.compile(r'^terraform\s*\{[\s\S]*?\n\}\s*\n', re.MULTILINE),
    re.compile(r'^provider\s+"okta"\s*\{[\s\S]*?\n\}\s*\n', re.MULTILINE),
    re.compile(r'^provider\s+"aws"\s*\{[\s\S]*?\n\}\s*\n', re.MULTILINE),
    re.compile(r'^provider\s+"google"\s*\{[\s\S]*?\n\}\s*\n', re.MULTILINE),
    re.compile(r'^variable\s+"okta_api_token"\s*\{[\s\S]*?\n\}\s*\n', re.MULTILINE),
    re.compile(r'^variable\s+"okta_org_name"\s*\{[\s\S]*?\n\}\s*\n', re.MULTILINE),
    re.compile(r'^variable\s+"okta_base_url"\s*\{[\s\S]*?\n\}\s*\n', re.MULTILINE),
    re.compile(r'^variable\s+"aws_region"\s*\{[\s\S]*?\n\}\s*\n', re.MULTILINE),
    re.compile(r'^variable\s+"gcp_project_id"\s*\{[\s\S]*?\n\}\s*\n', re.MULTILINE),
    re.compile(r'^variable\s+"gcp_region"\s*\{[\s\S]*?\n\}\s*\n', re.MULTILINE),
]


def derive_basename_from_intent(intent: dict | None) -> str:
    """Derive a filesystem-safe basename from a parsed intent dict so the
    Streamlit UI can auto-name per-prompt files when the user does not type
    one in. The parser already produces `intent["resource_name"]` as a
    snake_case identifier (e.g. "engineering", "hr_portal_workday",
    "gcp_bigquery_readonly"), which is exactly what we want for the file
    path.

    Sanitization: lowercase, replace any character not in [a-z0-9_] with `_`,
    collapse consecutive underscores, strip leading/trailing underscores.
    Empty input or missing resource_name returns "" (the legacy okta.tf path
    will be used).
    """
    if not intent:
        return ""
    name = intent.get("resource_name") or ""
    if not name:
        return ""
    sanitized = re.sub(r'[^a-z0-9_]+', '_', name.lower())
    sanitized = re.sub(r'_+', '_', sanitized)
    return sanitized.strip('_')


def strip_provider_boilerplate(hcl: str) -> str:
    """Strip the three boilerplate blocks (terraform / provider "okta" /
    variable "okta_api_token") from an HCL string.

    The patterns are anchored at column 0 with `re.MULTILINE`, and the lazy
    `[\\s\\S]*?` body matches across newlines until the first column-0 closing
    brace. Nested braces inside (e.g. `required_providers { okta = {} }`) do
    not interfere because their closing braces are indented, not at column 0.

    Idempotent: running on already-stripped input is a no-op.
    """
    if not hcl:
        return hcl
    out = hcl
    for pattern in _BOILERPLATE_PATTERNS:
        out = pattern.sub('', out)
    return out.lstrip('\n')
