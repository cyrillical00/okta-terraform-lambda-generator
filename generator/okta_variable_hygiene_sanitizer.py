"""Deterministic post-generation sanitizer for `variable "X" {}` hygiene.

Two failure modes addressed:

1. **Undeclared `var.X` references** — LLM uses `var.foo` without emitting
   the corresponding `variable "foo" {}` block. Surfaces as
   `Reference to undeclared input variable` on terraform validate.
   Observed on AUTH02. Fix: append stub `variable "X" {}` blocks.

2. **Interpolations in variable defaults** — LLM emits
   `default = "Welcome to ${org.name}"` inside a variable block. Terraform
   forbids expressions in input-variable defaults; init fails before
   reaching validate with a generic `Terraform encountered problems during
   initialisation` wrapper that masks the underlying error. Observed on
   EM01, ED05, COMP08. Fix: scrub the interpolation back to literal text
   (drop the `${...}` segments, leave the surrounding string).

Public API: `sanitize_okta_variable_hygiene(outputs)`.

Pure function. Standard library only. Idempotent.
"""

from __future__ import annotations

import re

_VAR_REFERENCE_RE = re.compile(r"\bvar\.([a-zA-Z_][a-zA-Z0-9_]*)")
_VARIABLE_DECL_RE = re.compile(r'^\s*variable\s+"([a-zA-Z_][a-zA-Z0-9_]*)"\s*\{', re.MULTILINE)

# Variable block with body capture for default-interpolation scrub.
_VARIABLE_BLOCK_RE = re.compile(
    r'(variable\s+"[a-zA-Z_][a-zA-Z0-9_]*"\s*\{)([\s\S]*?)(^\})',
    re.MULTILINE,
)
# Default = "string with ${expr} interpolation" — only the interpolation is bad,
# the surrounding literal should be kept (with the ${...} segment removed).
_DEFAULT_LINE_RE = re.compile(
    r'(^[ \t]*default\s*=\s*)("(?:[^"\\]|\\.)*")',
    re.MULTILINE,
)
_INTERPOLATION_RE = re.compile(r"\$\{[^}]+\}")

_HCL_KEYS = ("terraform_okta_hcl",)


def sanitize_okta_variable_hygiene(outputs: dict) -> dict:
    """Append stub `variable "X" {}` blocks for undeclared `var.X` refs and
    strip interpolations from `default = "..."` lines inside variable blocks.

    Args:
        outputs: Generator output dict. Only terraform_okta_hcl is touched.

    Returns:
        A new outputs dict (the input is not mutated). Two-pass: first strips
        interpolations from variable defaults (otherwise terraform init fails
        before reading the rest of the file), then appends stubs for any
        still-undeclared `var.X` references.
    """
    result = dict(outputs)
    for key in _HCL_KEYS:
        hcl = result.get(key, "") or ""
        if not hcl.strip():
            continue
        hcl = _strip_default_interpolations(hcl)
        hcl = _patch_missing_variables(hcl)
        result[key] = hcl
    return result


def _strip_default_interpolations(hcl: str) -> str:
    """For each variable block, scrub `${...}` interpolations from any
    `default = "..."` line. Terraform rejects expressions in default values
    at init time. The surrounding literal text is preserved so the variable
    still has a sensible string default."""

    def block_replacement(match: re.Match) -> str:
        opener = match.group(1)
        body = match.group(2)
        closer = match.group(3)

        def default_replacement(inner: re.Match) -> str:
            prefix = inner.group(1)
            literal = inner.group(2)
            scrubbed = _INTERPOLATION_RE.sub("", literal)
            return f"{prefix}{scrubbed}"

        new_body = _DEFAULT_LINE_RE.sub(default_replacement, body)
        return opener + new_body + closer

    return _VARIABLE_BLOCK_RE.sub(block_replacement, hcl)


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
