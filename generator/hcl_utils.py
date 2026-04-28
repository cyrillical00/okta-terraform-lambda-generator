"""Small HCL string utilities used by the generation pipeline.

Currently exposes one function: `strip_provider_boilerplate`, which removes
the `terraform {}`, `provider "okta" {}`, and `variable "okta_api_token" {}`
top-level blocks from a generated HCL string. Used when pushing per-prompt
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
    re.compile(r'^variable\s+"okta_api_token"\s*\{[\s\S]*?\n\}\s*\n', re.MULTILINE),
]


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
