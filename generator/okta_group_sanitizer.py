"""Deterministic post-generation sanitizer for hallucinated `data "okta_group"` blocks.

The LLM occasionally emits `data "okta_group" "X" { name = "Y" }` for groups
that do not exist in the live Okta org, often with a fabricated id in a
preceding `# Resolved from live environment, id: 00g...` comment. This causes
`terraform apply` to fail with `group with name "Y" does not exist`.

This module is the deterministic backstop that runs after the LLM 3-pass
refinement loop. It rewrites every hallucinated data block into a proper
`resource "okta_group"` create, and updates all `data.okta_group.X.*`
references throughout the generated HCL to point at the new resource.

Public API: `sanitize_okta_group_refs(outputs, live_groups)`.

Pure function. Standard library only. Idempotent. No-op when live_groups is
empty (no ground truth to compare against).
"""

from __future__ import annotations

import re

DATA_BLOCK_RE = re.compile(
    r'(?:^[ \t]*(\#[ \t]*Resolved from live environment[^\n]*)\n)?'
    r'(^[ \t]*data\s+"okta_group"\s+"([^"]+)"\s*\{'
    r'[ \t]*\n[ \t]*name\s*=\s*"([^"]+)"[ \t]*\n'
    r'[ \t]*\})',
    re.MULTILINE,
)

_HCL_KEYS = ("terraform_okta_hcl", "terraform_lambda_hcl", "optional_tf")


def sanitize_okta_group_refs(outputs: dict, live_groups: list[dict] | None) -> dict:
    """Rewrite hallucinated `data "okta_group"` blocks into `resource "okta_group"` creates.

    Args:
        outputs: Generator output dict. Keys of interest: `terraform_okta_hcl`,
            `terraform_lambda_hcl`, `optional_tf`. Other keys are passed through
            unchanged.
        live_groups: List of `{"id": ..., "name": ...}` dicts from
            `OktaClient.list_groups()`, typically reached via
            `st.session_state.env_context["okta"]["groups"]`. None or empty
            means "no ground truth available", which short-circuits to a no-op
            so we don't rewrite when Okta is disconnected.

    Returns:
        A new outputs dict (the input is not mutated). HCL strings are rewritten
        only where a `data "okta_group" "X" { name = "Y" }` block names a `Y`
        that does NOT appear verbatim in `live_groups`. References elsewhere in
        the HCL (`data.okta_group.X.id`) are also rewritten to match.

    Behavior is conservative: anything the strict regex does not recognize
    (data blocks with extra attributes, nested blocks, inline body comments) is
    left untouched. Better to under-sanitize than to corrupt working HCL.
    """
    if not live_groups:
        return outputs

    live_name_to_id = {
        g["name"]: g["id"]
        for g in live_groups
        if isinstance(g, dict) and "name" in g and "id" in g
    }
    if not live_name_to_id:
        return outputs

    result = dict(outputs)

    # Pass 1: rewrite the data blocks themselves in each HCL key.
    # Collect the labels of every block we rewrote, across keys, so refs in
    # other keys (e.g. data.okta_group.hr.id appearing in terraform_lambda_hcl
    # while the data block lives in terraform_okta_hcl) can be cleaned up too.
    rewritten_labels: set[str] = set()
    for key in _HCL_KEYS:
        hcl = result.get(key)
        if not hcl or not hcl.strip():
            continue
        new_hcl, labels = _rewrite_data_blocks(hcl, live_name_to_id)
        result[key] = new_hcl
        rewritten_labels.update(labels)

    # Pass 2: with every rewritten label known, scrub references across all
    # HCL keys. This must run even on keys with no data blocks of their own.
    if rewritten_labels:
        for key in _HCL_KEYS:
            hcl = result.get(key)
            if not hcl or not hcl.strip():
                continue
            result[key] = _rewrite_references(hcl, rewritten_labels)

    return result


def _rewrite_data_blocks(
    hcl: str, live_name_to_id: dict[str, str]
) -> tuple[str, set[str]]:
    """Rewrite invalid data blocks in this HCL string.

    Returns a tuple of (new_hcl, set_of_rewritten_labels). A label is added
    to the set when its data block was successfully rewritten into a resource
    block; callers use that set to drive cross-key reference rewriting.
    """

    rewrite_jobs: list[tuple[int, int, str, str]] = []
    for m in DATA_BLOCK_RE.finditer(hcl):
        comment = m.group(1)
        label = m.group(3)
        name = m.group(4)

        if name in live_name_to_id:
            continue

        block_end = m.end(2)
        replacement_start = m.start(1) if comment is not None else m.start(2)

        rewrite_jobs.append((replacement_start, block_end, label, name))

    if not rewrite_jobs:
        return hcl, set()

    out = hcl
    for replacement_start, block_end, label, name in sorted(
        rewrite_jobs, key=lambda j: j[0], reverse=True
    ):
        out = out[:replacement_start] + _build_resource_block(label, name) + out[block_end:]

    return out, {label for (_, _, label, _) in rewrite_jobs}


def _rewrite_references(hcl: str, labels: set[str]) -> str:
    """Rewrite `data.okta_group.<label>` refs to `okta_group.<label>` for every
    label in the set. Word-boundary anchored so `data.okta_group.foo.id` becomes
    `okta_group.foo.id` and bare `data.okta_group.foo` (in `depends_on` etc.)
    becomes `okta_group.foo`."""
    out = hcl
    for label in labels:
        ref_pattern = re.compile(r'\bdata\.okta_group\.' + re.escape(label) + r'\b')
        out = ref_pattern.sub(f'okta_group.{label}', out)
    return out


def _build_resource_block(label: str, name: str) -> str:
    description = _default_description_for(name)
    return (
        f'resource "okta_group" "{label}" {{\n'
        f'  name        = "{name}"\n'
        f'  description = "{description}"\n'
        f'}}'
    )


def _default_description_for(name: str) -> str:
    return f"{name} group"
