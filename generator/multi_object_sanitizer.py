"""Deterministic post-generation sanitizer for multi-object resource emission.

When a prompt enumerates multiple resources of the same type ("create three
groups: A, B, C"), the multi_object_detector attaches the parsed list to
`intent["instances"]` and the generator user-prompt template surfaces them
explicitly. Most of the time the LLM emits N resource blocks correctly. This
module is the deterministic backstop for the cases where it doesn't —
typically when the LLM emits a single block representing the first instance
and stops.

Logic:

1. No-op when intent.instances is absent or empty.
2. For each HCL key (`terraform_okta_hcl`, `terraform_lambda_hcl`,
   `terraform_gcp_hcl`, `terraform_jamf_hcl`), parse resource blocks via
   balanced-brace counting (not regex — JAMF policies have deeply nested
   `payloads { packages { package { ... } } }` shapes).
3. For each resource type in the HCL, check whether any emitted block's
   `name = "..."` attribute matches one of the instance names. Resource
   types with at least one matching block but fewer total blocks than the
   instance count are candidates for cloning.
4. For each candidate type, take the first matching block as a template,
   clone once per missing instance, substituting `name = "..."` and the
   resource label (slug derived from the instance name). Append the clones
   after the original block.

Public API: `sanitize_multi_object(outputs, intent)`.

Pure function. Standard library only. Idempotent. Conservative — only acts
when the candidate type is unambiguous and the template block is well-formed
(contains a `name = "..."` line).
"""

from __future__ import annotations

import re

_HCL_KEYS = (
    "terraform_okta_hcl",
    "terraform_lambda_hcl",
    "terraform_gcp_hcl",
    "terraform_jamf_hcl",
)

_RESOURCE_HEADER_RE = re.compile(
    r'resource\s+"([a-zA-Z_][\w]*)"\s+"([a-zA-Z_][\w-]*)"\s*\{'
)

_NAME_ATTR_RE = re.compile(
    r'(^[ \t]*name\s*=\s*)"([^"\n]+)"',
    re.MULTILINE,
)


def sanitize_multi_object(outputs: dict, intent: dict) -> dict:
    """Clone single-emitted resource blocks to match `intent.instances` count.

    Args:
        outputs: Generator output dict.
        intent: Intent dict; uses `intent["instances"]` (list of dicts with at
            minimum a `name` key) to determine the target count.

    Returns:
        A new outputs dict (the input is not mutated). HCL strings are
        rewritten only when the sanitizer identified an unambiguous
        under-emitted resource type.
    """
    instances = (intent or {}).get("instances") or []
    if len(instances) < 2:
        return outputs

    instance_names = [inst.get("name", "") for inst in instances if inst.get("name")]
    if len(instance_names) < 2:
        return outputs

    result = dict(outputs)
    for key in _HCL_KEYS:
        hcl = result.get(key, "") or ""
        if not hcl.strip():
            continue
        result[key] = _expand_under_emitted_blocks(hcl, instance_names)
    return result


def _expand_under_emitted_blocks(hcl: str, instance_names: list[str]) -> str:
    """For each resource type with at least one block whose name is in
    instance_names but with fewer blocks than len(instance_names), clone the
    first matching block to fill in the missing instances."""
    blocks = _parse_resource_blocks(hcl)
    if not blocks:
        return hcl

    # Group blocks by resource type. For each type, collect (label, body, span,
    # name_attr_value).
    by_type: dict[str, list[dict]] = {}
    for blk in blocks:
        by_type.setdefault(blk["type"], []).append(blk)

    # Find candidate types: any type with >=1 block matching an instance name
    # but total block count < len(instance_names).
    rewrite_jobs: list[tuple[int, int, str]] = []  # (start, end, replacement)

    for rtype, blks in by_type.items():
        matched_names = [b for b in blks if b["name_value"] in instance_names]
        if not matched_names:
            continue
        # Determine which instance names are already covered and which are missing.
        covered = {b["name_value"] for b in blks}
        missing = [n for n in instance_names if n not in covered]
        if not missing:
            # Every instance name has at least one block — LLM did the right
            # thing. No-op for this type.
            continue
        # Template: pick the LAST matching block so the inserted clones appear
        # immediately after it, preserving any inline order the LLM intended.
        template = matched_names[-1]
        clone_text = _build_clones(template, missing)
        # Append clones right after the template block's closing brace.
        insertion_point = template["span_end"]
        rewrite_jobs.append((insertion_point, insertion_point, "\n\n" + clone_text))

    if not rewrite_jobs:
        return hcl

    # Apply rewrites in reverse order so earlier indices remain valid.
    out = hcl
    for start, end, replacement in sorted(rewrite_jobs, key=lambda j: j[0], reverse=True):
        out = out[:start] + replacement + out[end:]
    return out


def _parse_resource_blocks(hcl: str) -> list[dict]:
    """Walk the HCL and return one record per `resource "..." "..." { ... }`
    block. Uses balanced-brace counting so nested sub-blocks don't confuse
    the boundary detection.

    Each record: {type, label, span_start, span_end, body, name_value}.
    """
    records: list[dict] = []
    for m in _RESOURCE_HEADER_RE.finditer(hcl):
        rtype = m.group(1)
        label = m.group(2)
        body_start = m.end()  # one past the opening `{`
        depth = 1
        i = body_start
        n = len(hcl)
        while i < n and depth > 0:
            c = hcl[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth != 0:
            # Unbalanced; skip to avoid corrupting downstream output.
            continue
        body = hcl[body_start:i]
        # Extract the name attribute value if present.
        name_match = _NAME_ATTR_RE.search(body)
        name_value = name_match.group(2) if name_match else ""
        records.append({
            "type": rtype,
            "label": label,
            "span_start": m.start(),
            "span_end": i + 1,  # position immediately after the closing `}`
            "body": body,
            "name_value": name_value,
        })
    return records


def _build_clones(template: dict, missing_names: list[str]) -> str:
    """Render one cloned resource block per missing name.

    The clone re-uses the template's body verbatim except:
      - the resource label is replaced with a slug of the new name
      - the `name = "..."` attribute value is replaced with the new name
    """
    parts: list[str] = []
    for new_name in missing_names:
        new_label = _slugify(new_name)
        # If the slug collides with the template's label, prefix to keep unique.
        if new_label == template["label"]:
            new_label = new_label + "_clone"
        new_body = _NAME_ATTR_RE.sub(
            lambda m: f'{m.group(1)}"{new_name}"',
            template["body"],
            count=1,
        )
        block = f'resource "{template["type"]}" "{new_label}" {{{new_body}}}'
        parts.append(block)
    return "\n\n".join(parts)


def _slugify(name: str) -> str:
    """Mirror generator/terraform_gen.py:_slugify_label. Lowercase alnum +
    underscore, leading-digit guard, never-empty."""
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", name.strip()).strip("_").lower()
    if not slug:
        slug = "instance"
    if slug[0].isdigit():
        slug = "i_" + slug
    return slug
