"""Deterministic post-generation sanitizer for okta_auth_server_policy_rule.

The okta provider v4.x exposes token-lifetime configuration via two int
attributes: `access_token_lifetime_minutes` and `refresh_token_lifetime_minutes`.
The bare attribute name `token_lifetime` does NOT exist in the v4 schema and
fails `terraform validate` with "Unsupported argument".

SECTION B of prompts.py (line 2095-2101) already lists `token_lifetime` as
FORBIDDEN, but the LLM still drifts on prompts like AP02 ("limiting token
lifetime to 1 hour"). This module is the deterministic backstop: it walks
every `resource "okta_auth_server_policy_rule"` block and rewrites
`token_lifetime = N` to `access_token_lifetime_minutes = N`.

Public API: `sanitize_okta_auth_server_policy_rule(outputs)`.

Pure function. Standard library only. Idempotent. Block-scoped (only
rewrites inside okta_auth_server_policy_rule resource blocks; other
resources stay untouched).
"""

from __future__ import annotations

import re

_RULE_BLOCK_RE = re.compile(
    r'(resource\s+"okta_auth_server_policy_rule"\s+"[^"]+"\s*\{)([\s\S]*?)(^\})',
    re.MULTILINE,
)

# Match `token_lifetime = <value>` on its own line, where <value> is an int
# literal, a var.X reference, or a numeric expression. The line is rewritten
# in place; the value is preserved verbatim, only the attribute name changes.
_TOKEN_LIFETIME_LINE_RE = re.compile(
    r'(^[ \t]*)token_lifetime(\s*=\s*[^\n]+\n)',
    re.MULTILINE,
)

_HCL_KEYS = ("terraform_okta_hcl",)


def sanitize_okta_auth_server_policy_rule(outputs: dict) -> dict:
    """Rewrite `token_lifetime` to `access_token_lifetime_minutes` inside
    every `resource "okta_auth_server_policy_rule"` block.

    Closes AP02 sampling drift: LLM occasionally emits `token_lifetime = N`
    despite SECTION B forbidding it. The okta v4.x provider requires
    `access_token_lifetime_minutes` (int) instead.

    Returns a new outputs dict. The input is not mutated. No-op when no
    `okta_auth_server_policy_rule` resource is present.
    """
    result = dict(outputs)
    for key in _HCL_KEYS:
        hcl = result.get(key, "") or ""
        if "okta_auth_server_policy_rule" not in hcl:
            continue
        result[key] = _rewrite_rule_blocks(hcl)
    return result


def _rewrite_rule_blocks(hcl: str) -> str:
    def block_replacement(match: re.Match) -> str:
        opener = match.group(1)
        body = match.group(2)
        closer = match.group(3)
        new_body = _TOKEN_LIFETIME_LINE_RE.sub(
            lambda m: f"{m.group(1)}access_token_lifetime_minutes{m.group(2)}",
            body,
        )
        return opener + new_body + closer

    return _RULE_BLOCK_RE.sub(block_replacement, hcl)
