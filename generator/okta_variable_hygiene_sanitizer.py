"""Deterministic post-generation sanitizer for missing `variable "X" {}` declarations.

The LLM occasionally references `var.X` inside generated HCL without emitting
the corresponding top-level `variable "X" {}` block. `terraform validate`
fails with `Reference to undeclared input variable`. Observed on AUTH02
(`var.auth_server_name`, `var.auth_server_description` referenced but never
declared).

This sanitizer scans `terraform_okta_hcl` for every `var.X` reference and
appends a stub `variable "X" {}` declaration for any name lacking one. The
stubs carry only a description; no type or default is set, which keeps
`terraform validate` green without committing the runtime layer to a specific
type the LLM might have intended differently.

Public API: `sanitize_okta_variable_hygiene(outputs)`.

Pure function. Standard library only. Idempotent.
"""

from __future__ import annotations

import re

_VAR_REFERENCE_RE = re.compile(r"\bvar\.([a-zA-Z_][a-zA-Z0-9_]*)")
_VARIABLE_DECL_RE = re.compile(r'^\s*variable\s+"([a-zA-Z_][a-zA-Z0-9_]*)"\s*\{', re.MULTILINE)

_HCL_KEYS = ("terraform_okta_hcl",)


def sanitize_okta_variable_hygiene(outputs: dict) -> dict:
    """Append stub `variable "X" {}` blocks for every undeclared `var.X` reference.

    Args:
        outputs: Generator output dict. Only terraform_okta_hcl is touched.

    Returns:
        A new outputs dict (the input is not mutated). For every `var.X`
        reference whose `variable "X" {}` block is absent, a stub declaration
        is appended at the end of the HCL with an auto-generated description.
    """
    result = dict(outputs)
    for key in _HCL_KEYS:
        hcl = result.get(key, "") or ""
        if not hcl.strip():
            continue
        result[key] = _patch_missing_variables(hcl)
    return result


def _patch_missing_variables(hcl: str) -> str:
    refs = {m.group(1) for m in _VAR_REFERENCE_RE.finditer(hcl)}
    if not refs:
        return hcl
    decls = {m.group(1) for m in _VARIABLE_DECL_RE.finditer(hcl)}
    missing = sorted(refs - decls)
    if not missing:
        return hcl

    appended_blocks = [_stub_variable_block(name) for name in missing]
    suffix = "\n\n" + "\n\n".join(appended_blocks) + "\n"
    if hcl.endswith("\n"):
        return hcl.rstrip("\n") + suffix
    return hcl + suffix


def _stub_variable_block(name: str) -> str:
    return (
        f'variable "{name}" {{\n'
        f'  description = "Auto-declared placeholder for var.{name}; set a value before apply."\n'
        f'}}'
    )
