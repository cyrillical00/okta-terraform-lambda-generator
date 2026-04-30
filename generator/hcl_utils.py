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


_TERRAFORM_BLOCK_RE = re.compile(
    r'^terraform\s*\{[\s\S]*?\n\}\s*\n?', re.MULTILINE,
)


def _find_balanced_block(hcl: str, header_pattern: str) -> tuple[int, int, int, int] | None:
    """Find a `<header> { ... }` block and return (header_start, body_start,
    body_end, block_end) using brace counting. Returns None if not found.
    `header_pattern` is a regex matched at any position; it must include the
    opening `{`. body_start is one past that `{`; body_end is the position of
    the matching closing `}`; block_end is body_end + 1.
    """
    m = re.search(header_pattern, hcl, flags=re.MULTILINE)
    if not m:
        return None
    open_idx = hcl.rfind('{', m.start(), m.end())
    if open_idx == -1:
        return None
    depth = 1
    i = open_idx + 1
    while i < len(hcl) and depth > 0:
        if hcl[i] == '{':
            depth += 1
        elif hcl[i] == '}':
            depth -= 1
        i += 1
    if depth != 0:
        return None
    return m.start(), open_idx + 1, i - 1, i


_PROVIDER_ENTRY_NAME_RE = re.compile(r'^\s*(\w+)\s*=\s*\{', re.MULTILINE)


def _split_provider_entries(body: str) -> list[tuple[str, str]]:
    """Split a `required_providers` body into [(name, full_entry_text)]
    using brace counting. Returns entries with their original whitespace.
    """
    entries: list[tuple[str, str]] = []
    i = 0
    while i < len(body):
        m = re.match(r'\s*(\w+)\s*=\s*\{', body[i:])
        if not m:
            i += 1
            continue
        name = m.group(1)
        entry_start = i + len(m.group(0)) - 1  # position of the opening `{`
        depth = 1
        j = entry_start + 1
        while j < len(body) and depth > 0:
            if body[j] == '{':
                depth += 1
            elif body[j] == '}':
                depth -= 1
            j += 1
        if depth != 0:
            break
        entries.append((name, body[i + len(m.group(0)) - len(m.group(0).lstrip()):j].lstrip()))
        i = j
    return entries


def merge_terraform_blocks(primary_hcl: str, secondary_hcl: str) -> tuple[str, str]:
    """Merge `required_providers` entries from secondary's `terraform {}` block
    into primary's, then strip the entire `terraform {}` block from secondary.

    Used for composite output modes (e.g. Okta + GCP) where two generated HCL
    files would otherwise both declare a `terraform { required_providers {} }`
    block, causing terraform init to fail with "Duplicate required providers
    configuration" when the files coexist in the same module.

    Behavior:
      * If either input lacks a `terraform {}` block, returns the inputs
        unchanged (no-op).
      * Provider entries already present in primary are not duplicated.
      * Provider entries in secondary that are not in primary are appended
        into primary's `required_providers` body, indented to match.
      * Whitespace and indentation in primary's existing block are preserved.

    Idempotent: applying twice yields the same result as once. Pure function.
    Standard library only.
    """
    if not primary_hcl or not secondary_hcl:
        return primary_hcl, secondary_hcl

    primary_tf = _find_balanced_block(primary_hcl, r'^terraform\s*\{')
    secondary_tf = _find_balanced_block(secondary_hcl, r'^terraform\s*\{')
    if not primary_tf or not secondary_tf:
        return primary_hcl, secondary_hcl

    secondary_rp = _find_balanced_block(secondary_hcl[secondary_tf[1]:secondary_tf[2]], r'required_providers\s*\{')
    if not secondary_rp:
        new_secondary = (secondary_hcl[:secondary_tf[0]] + secondary_hcl[secondary_tf[3]:]).lstrip('\n')
        return primary_hcl, new_secondary

    rp_offset = secondary_tf[1]
    secondary_rp_body = secondary_hcl[rp_offset + secondary_rp[1]: rp_offset + secondary_rp[2]]
    secondary_entries = dict(_split_provider_entries(secondary_rp_body))
    if not secondary_entries:
        new_secondary = (secondary_hcl[:secondary_tf[0]] + secondary_hcl[secondary_tf[3]:]).lstrip('\n')
        return primary_hcl, new_secondary

    primary_rp = _find_balanced_block(primary_hcl[primary_tf[1]:primary_tf[2]], r'required_providers\s*\{')
    if not primary_rp:
        new_secondary = (secondary_hcl[:secondary_tf[0]] + secondary_hcl[secondary_tf[3]:]).lstrip('\n')
        return primary_hcl, new_secondary

    p_rp_offset = primary_tf[1]
    primary_rp_body = primary_hcl[p_rp_offset + primary_rp[1]: p_rp_offset + primary_rp[2]]
    primary_existing = {name for name, _ in _split_provider_entries(primary_rp_body)}
    to_add = [(name, entry) for name, entry in secondary_entries.items() if name not in primary_existing]

    if not to_add:
        new_secondary = (secondary_hcl[:secondary_tf[0]] + secondary_hcl[secondary_tf[3]:]).lstrip('\n')
        return primary_hcl, new_secondary

    indent_match = re.search(r'\n([ \t]+)\w+\s*=\s*\{', primary_rp_body)
    indent = indent_match.group(1) if indent_match else '    '

    additions_text = ''
    for _, entry in to_add:
        entry_lines = entry.rstrip().split('\n')
        for k, line in enumerate(entry_lines):
            if k == 0:
                additions_text += '\n' + indent + line.lstrip()
            elif line.strip():
                stripped = line.lstrip()
                additions_text += '\n' + indent + stripped if entry_lines[0].lstrip().endswith('{') else '\n' + line
                additions_text = additions_text.rstrip() + '\n' + indent + stripped[len(indent):] if False else additions_text
            else:
                additions_text += '\n'
        # simpler: just re-emit with consistent indent based on first-line
        # (above is mid-edit residue) — finalize below

    # Simpler re-indent: dedent each entry then re-indent at primary's indent.
    additions_text = ''
    for _, entry in to_add:
        entry_lines = entry.rstrip().split('\n')
        # detect minimum indent of non-blank lines after the first
        non_first_indents = [
            len(l) - len(l.lstrip()) for l in entry_lines[1:] if l.strip()
        ]
        base_indent = min(non_first_indents) if non_first_indents else 0
        rebuilt = []
        for k, line in enumerate(entry_lines):
            if k == 0:
                rebuilt.append(indent + line.lstrip())
            elif not line.strip():
                rebuilt.append('')
            else:
                rebuilt.append(indent + line[base_indent:] if len(line) >= base_indent else indent + line.lstrip())
        additions_text += '\n' + '\n'.join(rebuilt)

    new_primary_rp_body = primary_rp_body.rstrip() + additions_text + '\n  '
    new_primary = (
        primary_hcl[: p_rp_offset + primary_rp[1]]
        + new_primary_rp_body
        + primary_hcl[p_rp_offset + primary_rp[2]:]
    )
    new_secondary = (secondary_hcl[:secondary_tf[0]] + secondary_hcl[secondary_tf[3]:]).lstrip('\n')
    return new_primary, new_secondary
