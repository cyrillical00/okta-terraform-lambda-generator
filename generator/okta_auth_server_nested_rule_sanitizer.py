"""Deterministic post-generation sanitizer for AUTH05-class nested-resource drift.

HCL does not allow `resource` blocks nested inside other `resource` blocks;
they must be siblings at the top level. The okta_auth_server_policy and
okta_auth_server_policy_rule resources have a parent-child relationship at
the API level (policies own rules), and the LLM occasionally expresses that
via block nesting:

    resource "okta_auth_server_policy" "p" {
        ...
        resource "okta_auth_server_policy_rule" "r" {
            policy_id = okta_auth_server_policy.p.id
            ...
        }
    }

Terraform validate rejects this with `Error: Unsupported block type: Blocks
of type "resource" are not expected here.` (this surfaced live as the
sustained AUTH05 drift after Phase 20 shipped). The fix is structural: the
two resources are SIBLINGS at top level, linked via `policy_id`.

This sanitizer walks every `resource "okta_auth_server_policy"` block, finds
any nested `resource "okta_auth_server_policy_rule"` block inside, removes
it from the parent body, and inserts it immediately AFTER the parent's
closing brace at top level. Order-preserving: multiple nested rules come
out in the order they appeared in the parent.

Public API: `sanitize_okta_auth_server_nested_rule(outputs)`.

Pure function. Standard library only. Idempotent. Block-scoped (only
operates on okta_auth_server_policy / _rule pairs; other resources stay
untouched).
"""

from __future__ import annotations

import re

_POLICY_OPENER_RE = re.compile(
    r'resource\s+"okta_auth_server_policy"\s+"[^"]+"\s*\{',
)

_NESTED_RULE_OPENER_RE = re.compile(
    r'^([ \t]*)resource\s+"okta_auth_server_policy_rule"\s+"[^"]+"\s*\{',
    re.MULTILINE,
)

_HCL_KEYS = ("terraform_okta_hcl",)


def sanitize_okta_auth_server_nested_rule(outputs: dict) -> dict:
    """Hoist any nested okta_auth_server_policy_rule out of its parent
    okta_auth_server_policy block to top level.

    Closes AUTH05 structural drift: LLM occasionally nests the rule inside
    the policy block. Terraform rejects nested `resource` blocks; they must
    be siblings linked via `policy_id`.

    Returns a new outputs dict. The input is not mutated. No-op when no
    okta_auth_server_policy block contains a nested rule.
    """
    result = dict(outputs)
    for key in _HCL_KEYS:
        hcl = result.get(key, "") or ""
        if "okta_auth_server_policy" not in hcl:
            continue
        if 'resource "okta_auth_server_policy_rule"' not in hcl:
            continue
        result[key] = _hoist_nested_rules(hcl)
    return result


def _find_matching_brace(text: str, open_pos: int) -> int:
    """Given the position of an opening `{`, return the position of the
    matching closing `}`. Returns -1 if not balanced. Treats `{` / `}`
    inside double-quoted strings as literal so they don't disturb the
    depth counter; line comments (#, //) are also respected."""
    depth = 0
    i = open_pos
    n = len(text)
    in_string = False
    in_line_comment = False
    while i < n:
        ch = text[i]
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_string:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == "#":
            in_line_comment = True
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _hoist_nested_rules(hcl: str) -> str:
    """One pass: find every okta_auth_server_policy block, walk its body
    looking for nested okta_auth_server_policy_rule blocks, extract them,
    and append them as top-level resources after the parent. Idempotent
    because clean HCL has no nested rules and the function short-circuits
    on the substring check upstream."""
    # We rebuild the output left-to-right so positional indexes stay
    # consistent within a single pass.
    out_chunks: list[str] = []
    cursor = 0
    hoisted: list[str] = []

    for parent_match in _POLICY_OPENER_RE.finditer(hcl):
        opener_start = parent_match.start()
        opener_end = parent_match.end()
        brace_pos = opener_end - 1  # the `{` is the last char of the opener
        close_pos = _find_matching_brace(hcl, brace_pos)
        if close_pos == -1:
            # Unbalanced; bail on this parent, keep the original text.
            continue

        body = hcl[opener_end:close_pos]
        new_body, extracted = _extract_nested_rules(body)
        if not extracted:
            continue

        # Emit text up to the parent opener, then the rewritten parent
        # block, then defer the extracted rules until after the parent.
        out_chunks.append(hcl[cursor:opener_end])
        out_chunks.append(new_body)
        out_chunks.append("}")
        for rule_block in extracted:
            hoisted.append("\n\n" + rule_block.lstrip("\n"))
        cursor = close_pos + 1

    if cursor == 0:
        # No rewrites happened; return unchanged.
        return hcl

    out_chunks.append(hcl[cursor:])
    rebuilt = "".join(out_chunks)
    if hoisted:
        rebuilt = rebuilt + "".join(hoisted)
        # Make sure the file ends with exactly one trailing newline.
        rebuilt = rebuilt.rstrip("\n") + "\n"
    return rebuilt


def _extract_nested_rules(body: str) -> tuple[str, list[str]]:
    """Within the body of a single okta_auth_server_policy block, find any
    nested `resource "okta_auth_server_policy_rule" "..."` blocks, remove
    them, and return (cleaned_body, list_of_extracted_block_texts).

    Extracted blocks are dedented (the parent body is typically indented
    one level deeper than top-level) so they read cleanly as top-level
    resources."""
    extracted: list[str] = []
    out = []
    cursor = 0

    for rule_match in _NESTED_RULE_OPENER_RE.finditer(body):
        opener_start = rule_match.start()
        opener_end = rule_match.end()
        leading_indent = rule_match.group(1)
        brace_pos = opener_end - 1
        close_pos = _find_matching_brace(body, brace_pos)
        if close_pos == -1:
            continue

        # `opener_start` is already at start-of-line because the regex
        # anchors with `^` in MULTILINE and captures the indent into group
        # 1; do NOT subtract len(leading_indent). Back up one more char
        # to consume the preceding line's newline so we don't leave a
        # dangling blank line in the parent body where the rule used to be.
        line_start = opener_start
        if line_start > 0 and body[line_start - 1] == "\n":
            line_start -= 1

        out.append(body[cursor:line_start])

        # Build the dedented rule block. The opener line had `leading_indent`
        # spaces, so strip that prefix from every line of the captured block.
        raw_block = body[opener_start:close_pos + 1]
        dedented = _dedent(raw_block, leading_indent)
        extracted.append(dedented)

        # Advance cursor past the trailing newline if present so the parent
        # body doesn't keep a blank line where the rule used to be.
        cursor = close_pos + 1
        if cursor < len(body) and body[cursor] == "\n":
            cursor += 1

    out.append(body[cursor:])
    return "".join(out), extracted


def _dedent(text: str, indent: str) -> str:
    """Remove `indent` from the start of every line that begins with it.
    Lines that don't start with `indent` are left alone (they may already
    be at top-level indentation, e.g. a `}` that closed the block)."""
    if not indent:
        return text
    lines = text.split("\n")
    out_lines = []
    for line in lines:
        if line.startswith(indent):
            out_lines.append(line[len(indent):])
        else:
            out_lines.append(line)
    return "\n".join(out_lines)
