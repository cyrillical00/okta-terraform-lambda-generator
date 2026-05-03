"""Deterministic post-generation sanitizer for hallucinated SCIM/provisioning
attributes on `okta_app_saml` and `okta_app_oauth` resources.

The Okta Terraform provider v4.x has no SCIM provisioning support on app
resources. SCIM is a UI-only operation (Admin Console -> Applications ->
[App] -> Provisioning tab). The model nevertheless emits `provisioning {}`
blocks under prompt pressure when the user asks for "SCIM provisioning",
and `terraform validate` then fails with `Unsupported argument`.

This module is the deterministic backstop that runs after the LLM 3-pass
refinement loop. It strips:
  * any `provisioning { ... }` block,
  * forbidden single-line attributes (`provisioning_type`, `scim_enabled`,
    `scim_url`, `scim_settings`, `scim_connector`)
from inside any `resource "okta_app_saml"` or `resource "okta_app_oauth"`
block. When a `provisioning` block is stripped and the resource has no
existing `# NOTE:` comment immediately above it, a short NOTE comment is
inserted pointing the user to the Admin Console Provisioning tab (matching
the canonical example in `prompts.py`).

Public API: `sanitize_okta_app_scim_refs(outputs)`.

Pure function. Standard library only. Idempotent.
"""

from __future__ import annotations

import re

_APP_BLOCK_RE = re.compile(
    r'(resource\s+"(?:okta_app_saml|okta_app_oauth)"\s+"[^"]+"\s*\{)([\s\S]*?)(^\})',
    re.MULTILINE,
)

_PROVISIONING_BLOCK_RE = re.compile(
    r'^[ \t]*provisioning\s*\{[\s\S]*?^[ \t]*\}\s*\n',
    re.MULTILINE,
)

_FORBIDDEN_ATTR_RE = re.compile(
    r'^[ \t]*(provisioning_type|scim_enabled|scim_url|scim_settings|scim_connector)[ \t]*=[^\n]*\n',
    re.MULTILINE,
)

_RESOURCE_HEADER_RE = re.compile(
    r'(?:^|\n)((?:[ \t]*#[^\n]*\n)*)([ \t]*resource\s+"(?:okta_app_saml|okta_app_oauth)"\s+"[^"]+"\s*\{)',
)

_HCL_KEYS = ("terraform_okta_hcl", "optional_tf")

_NOTE_COMMENT = (
    "# NOTE: SCIM provisioning for this app cannot be configured via the v4.x Okta Terraform provider.\n"
    "# Configure it in the Okta Admin Console: Applications -> [App Label] -> Provisioning tab.\n"
)


def sanitize_okta_app_scim_refs(outputs: dict) -> dict:
    """Strip `provisioning { ... }` blocks and forbidden SCIM attributes from
    every `resource "okta_app_saml"` and `resource "okta_app_oauth"` block in
    the generated HCL. Insert a NOTE comment above resources where a
    provisioning block was stripped, unless one already exists.

    Returns a new outputs dict. The input is not mutated. No-op when no
    relevant app resource is present.
    """
    result = dict(outputs)
    for key in _HCL_KEYS:
        hcl = result.get(key)
        if not hcl or not hcl.strip():
            continue
        if "okta_app_saml" not in hcl and "okta_app_oauth" not in hcl:
            continue
        result[key] = _sanitize(hcl)
    return result


def _sanitize(hcl: str) -> str:
    stripped_resources: set[str] = set()

    def _scrub_body(match: re.Match) -> str:
        header, body, footer = match.group(1), match.group(2), match.group(3)
        new_body = _PROVISIONING_BLOCK_RE.sub("", body)
        if new_body != body:
            stripped_resources.add(header)
        new_body = _FORBIDDEN_ATTR_RE.sub("", new_body)
        return header + new_body + footer

    cleaned = _APP_BLOCK_RE.sub(_scrub_body, hcl)
    if not stripped_resources:
        return cleaned
    return _insert_notes(cleaned, stripped_resources)


def _insert_notes(hcl: str, stripped_resources: set[str]) -> str:
    def _replace(match: re.Match) -> str:
        existing_comments = match.group(1) or ""
        header_line = match.group(2)
        normalized_header = header_line.strip()
        target_headers = {h.strip() for h in stripped_resources}
        if normalized_header not in target_headers:
            return match.group(0)
        if "SCIM provisioning" in existing_comments:
            return match.group(0)
        prefix = "\n" if match.group(0).startswith("\n") else ""
        return f"{prefix}{existing_comments}{_NOTE_COMMENT}{header_line}"

    return _RESOURCE_HEADER_RE.sub(_replace, hcl)
