"""Deterministic post-generation sanitizer for hallucinated okta_* data sources.

The LLM occasionally invents non-existent `data "okta_<type>"` data sources
that fail `terraform validate` with `Invalid data source`. Observed: COMP04
emitting `data "okta_auth_server_default_policy" "default"` (does not exist
in the okta/okta v4.20.0 provider) and then referencing
`data.okta_auth_server_default_policy.default.id` in a policy rule.

This sanitizer enforces a blocklist of known-hallucinated data source types.
For each match it:

1. Removes the offending `data "okta_<type>" "label" { ... }` block.
2. Rewrites every `data.okta_<type>.label.<attr>` reference to a
   `var.<placeholder>` token. The variable-hygiene sanitizer
   (okta_variable_hygiene_sanitizer.py) then appends a stub declaration so
   `terraform validate` resolves cleanly. The user can supply real values at
   apply time.

Public API: `sanitize_okta_data_source_refs(outputs)`.

Pure function. Standard library only. Idempotent.
"""

from __future__ import annotations

import re

# Blocklist of data source types observed to be hallucinated. Expand as new
# regressions surface. Each entry is the type as it appears in the HCL —
# without the `okta_` prefix removed.
_HALLUCINATED_TYPES: tuple[str, ...] = (
    "okta_auth_server_default_policy",
)

_HCL_KEYS = ("terraform_okta_hcl",)


def sanitize_okta_data_source_refs(outputs: dict) -> dict:
    """Strip hallucinated okta_* data source blocks and rewrite their references.

    Args:
        outputs: Generator output dict. Only terraform_okta_hcl is touched.

    Returns:
        A new outputs dict (the input is not mutated). For every blocklisted
        data source, the data block is removed and every dotted-reference is
        rewritten to a placeholder `var.<type>_<label>_<attr>` token.
    """
    result = dict(outputs)
    for key in _HCL_KEYS:
        hcl = result.get(key, "") or ""
        if not hcl.strip():
            continue
        for type_name in _HALLUCINATED_TYPES:
            if type_name not in hcl:
                continue
            hcl = _strip_data_blocks(hcl, type_name)
            hcl = _rewrite_references(hcl, type_name)
        result[key] = hcl
    return result


def _strip_data_blocks(hcl: str, type_name: str) -> str:
    """Remove every `data "<type_name>" "X" { ... }` block.

    Greedy brace-matching is unnecessary here because the LLM-generated blocks
    use simple single-level braces. The block regex anchors on the closing
    `^}` line at column zero.
    """
    block_re = re.compile(
        r'(?:^[ \t]*#[^\n]*\n)?'
        r'^[ \t]*data\s+"' + re.escape(type_name) + r'"\s+"[^"]+"\s*\{'
        r'[\s\S]*?'
        r'^\}\s*\n?',
        re.MULTILINE,
    )
    return block_re.sub("", hcl)


def _rewrite_references(hcl: str, type_name: str) -> str:
    """Rewrite `data.<type>.<label>.<attr>` -> `var.<type_short>_<label>_<attr>`."""
    type_short = type_name.removeprefix("okta_")
    ref_re = re.compile(
        r'\bdata\.' + re.escape(type_name) + r'\.'
        r'([a-zA-Z_][a-zA-Z0-9_-]*)\.'
        r'([a-zA-Z_][a-zA-Z0-9_]*)'
    )

    def replacement(match: re.Match) -> str:
        label = match.group(1)
        attr = match.group(2)
        return f"var.{type_short}_{label}_{attr}"

    return ref_re.sub(replacement, hcl)
