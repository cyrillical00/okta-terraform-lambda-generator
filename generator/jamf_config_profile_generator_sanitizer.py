"""Deterministic post-generation sanitizer for
jamfpro_macos_configuration_profile_plist_generator.

The deploymenttheory/jamfpro v0.37 schema requires five header attributes
inside the `payloads {}` block plus a `payload_content {}` sub-block with
four required fields. SECTION D of prompts.py (line 1351-1422) teaches the
shape, but the LLM occasionally omits one or more required header attrs
on JF04-style prompts ("JAMF macOS configuration profile for our corporate
Wi-Fi settings").

Required header attrs inside `payloads {}`:
- payload_description_header (string)
- payload_enabled_header (bool)
- payload_organization_header (string)
- payload_type_header (string)
- payload_version_header (number)

Required fields inside `payload_content {}`:
- payload_enabled (bool)
- payload_organization (string)
- payload_type (string)
- payload_version (number)

This module is the deterministic backstop: it walks every
`resource "jamfpro_macos_configuration_profile_plist_generator"` block and
auto-inserts any missing required header attr or the entire
`payload_content {}` sub-block when missing. Auto-inserted lines carry the
`# auto-filled by Phase 20 sanitizer` comment so the user can diff them
against the original.

Public API: `sanitize_jamf_config_profile_generator(outputs)`.

Pure function. Standard library only. Idempotent. Block-scoped (only
rewrites inside the target resource type).
"""

from __future__ import annotations

import re

_PROFILE_BLOCK_RE = re.compile(
    r'(resource\s+"jamfpro_macos_configuration_profile_plist_generator"\s+"[^"]+"\s*\{)'
    r'([\s\S]*?)(^\})',
    re.MULTILINE,
)

# Match the `payloads {}` block. Uses a balanced indentation pattern: the
# opener line is `<indent>payloads {`, the closer is the matching `<indent>}`.
_PAYLOADS_BLOCK_RE = re.compile(
    r'(^([ \t]*)payloads\s*\{\n)([\s\S]*?)(^\2\})',
    re.MULTILINE,
)

# Required header attributes (name -> default value literal).
_REQUIRED_HEADERS: list[tuple[str, str]] = [
    ("payload_description_header",  '"Configuration profile"'),
    ("payload_enabled_header",      "true"),
    ("payload_organization_header", "var.jamf_organization"),
    ("payload_type_header",         '"Configuration"'),
    ("payload_version_header",      "1"),
]

# Required payload_content fields (name -> default value literal).
_REQUIRED_CONTENT_FIELDS: list[tuple[str, str]] = [
    ("payload_enabled",      "true"),
    ("payload_organization", "var.jamf_organization"),
    ("payload_type",         '"Configuration"'),
    ("payload_version",      "1"),
]

_HCL_KEYS = ("terraform_jamf_hcl",)


def sanitize_jamf_config_profile_generator(outputs: dict) -> dict:
    """Auto-fill missing required headers and payload_content sub-block in
    every `jamfpro_macos_configuration_profile_plist_generator` resource.

    Closes JF04 sampling drift: LLM occasionally omits one or more of the
    five required header attrs or the entire payload_content {} sub-block
    despite SECTION D teaching the shape.

    Returns a new outputs dict. The input is not mutated. No-op when no
    `jamfpro_macos_configuration_profile_plist_generator` resource is
    present.
    """
    result = dict(outputs)
    for key in _HCL_KEYS:
        hcl = result.get(key, "") or ""
        if "jamfpro_macos_configuration_profile_plist_generator" not in hcl:
            continue
        result[key] = _rewrite_profile_blocks(hcl)
    return result


def _rewrite_profile_blocks(hcl: str) -> str:
    def block_replacement(match: re.Match) -> str:
        opener = match.group(1)
        body = match.group(2)
        closer = match.group(3)
        new_body = _PAYLOADS_BLOCK_RE.sub(_fix_payloads_block, body)
        return opener + new_body + closer

    return _PROFILE_BLOCK_RE.sub(block_replacement, hcl)


def _fix_payloads_block(match: re.Match) -> str:
    opener_line = match.group(1)
    indent = match.group(2)
    inner = match.group(3)
    closer_line = match.group(4)

    inner_indent = indent + "  "

    # Step 1: insert missing header attributes.
    missing_headers: list[tuple[str, str]] = []
    for name, default in _REQUIRED_HEADERS:
        # Use a regex that matches only the top-level attribute line, not
        # the corresponding payload_content field (which lacks the _header
        # suffix and could not collide).
        attr_re = re.compile(rf'^[ \t]*{re.escape(name)}\s*=', re.MULTILINE)
        if not attr_re.search(inner):
            missing_headers.append((name, default))

    if missing_headers:
        # Compute longest header name in the missing set so the `=` aligns
        # roughly. Conservative formatting; one-space-after-name is also
        # acceptable HCL and avoids fighting with terraform fmt.
        header_lines = [
            f"{inner_indent}{name} = {default}  # auto-filled by Phase 20 sanitizer\n"
            for name, default in missing_headers
        ]
        # Prepend missing headers at the start of the payloads body so they
        # sit ahead of payload_content {} sub-block when both are present.
        inner = "".join(header_lines) + inner

    # Step 2: insert missing payload_content {} sub-block if absent.
    if not re.search(r'^[ \t]*payload_content\s*\{', inner, re.MULTILINE):
        content_indent = inner_indent + "  "
        content_lines = [
            f"\n{inner_indent}payload_content {{  # auto-filled by Phase 20 sanitizer\n",
        ]
        for name, default in _REQUIRED_CONTENT_FIELDS:
            content_lines.append(f"{content_indent}{name} = {default}\n")
        content_lines.append(f"{inner_indent}}}\n")
        # Append the synthesised sub-block at the end of the payloads body.
        if not inner.endswith("\n"):
            inner = inner + "\n"
        inner = inner + "".join(content_lines)
    else:
        # Sub-block exists; fill any missing required fields inside it.
        inner = _PAYLOAD_CONTENT_BLOCK_RE.sub(_fix_payload_content_body, inner)

    return opener_line + inner + closer_line


_PAYLOAD_CONTENT_BLOCK_RE = re.compile(
    r'(^([ \t]*)payload_content\s*\{\n)([\s\S]*?)(^\2\})',
    re.MULTILINE,
)


def _fix_payload_content_body(match: re.Match) -> str:
    opener_line = match.group(1)
    indent = match.group(2)
    inner = match.group(3)
    closer_line = match.group(4)
    inner_indent = indent + "  "

    missing: list[tuple[str, str]] = []
    for name, default in _REQUIRED_CONTENT_FIELDS:
        attr_re = re.compile(rf'^[ \t]*{re.escape(name)}\s*=', re.MULTILINE)
        if not attr_re.search(inner):
            missing.append((name, default))

    if missing:
        added = "".join(
            f"{inner_indent}{name} = {default}  # auto-filled by Phase 20 sanitizer\n"
            for name, default in missing
        )
        inner = added + inner

    return opener_line + inner + closer_line
