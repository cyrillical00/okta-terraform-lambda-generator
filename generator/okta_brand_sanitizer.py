"""Deterministic post-generation sanitizer for hallucinated `okta_brand` attributes.

The Okta Terraform provider's `okta_brand` resource does NOT support `logo`,
`primary_color`, or `secondary_color` (logo upload is an Admin Console
operation; colors are not exposed via Terraform). The LLM nevertheless emits
these in dog-food runs — terraform validate then fails with
`Unsupported argument`.

This module is the deterministic backstop that runs after the LLM 3-pass
refinement loop. It strips the forbidden attribute lines from inside any
`resource "okta_brand"` block while leaving other attributes intact.

Public API: `sanitize_okta_brand_refs(outputs)`.

Pure function. Standard library only. Idempotent.
"""

from __future__ import annotations

import re

_BRAND_BLOCK_RE = re.compile(
    r'(resource\s+"okta_brand"\s+"[^"]+"\s*\{)([\s\S]*?)(^\})',
    re.MULTILINE,
)

_FORBIDDEN_ATTR_RE = re.compile(
    r'^[ \t]*(logo|primary_color|secondary_color)[ \t]*=[^\n]*\n',
    re.MULTILINE,
)

_FORBIDDEN_BLOCK_RE = re.compile(
    r'^[ \t]*logo\s*\{[\s\S]*?^[ \t]*\}\s*\n',
    re.MULTILINE,
)

_HCL_KEYS = ("terraform_okta_hcl", "optional_tf")


def sanitize_okta_brand_refs(outputs: dict) -> dict:
    """Strip forbidden attributes (logo, primary_color, secondary_color) from
    every `resource "okta_brand"` block in the generated HCL.

    Returns a new outputs dict. The input is not mutated. No-op when no
    `okta_brand` resource is present.
    """
    result = dict(outputs)
    for key in _HCL_KEYS:
        hcl = result.get(key)
        if not hcl or not hcl.strip():
            continue
        if 'okta_brand' not in hcl:
            continue
        result[key] = _strip_forbidden(hcl)
    return result


def _strip_forbidden(hcl: str) -> str:
    def _scrub_body(match: re.Match) -> str:
        header, body, footer = match.group(1), match.group(2), match.group(3)
        body = _FORBIDDEN_BLOCK_RE.sub('', body)
        body = _FORBIDDEN_ATTR_RE.sub('', body)
        return header + body + footer

    return _BRAND_BLOCK_RE.sub(_scrub_body, hcl)
