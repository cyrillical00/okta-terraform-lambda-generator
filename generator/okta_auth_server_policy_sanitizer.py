"""Deterministic post-generation sanitizer for okta_auth_server_policy.

The okta provider v4.x exposes the client-allow-list attribute as
`client_whitelist` (list of strings). The bare attribute name `clients`
does NOT exist in the v4 schema and fails `terraform validate` with
"Unsupported argument".

SECTION B of prompts.py (line 2090-2093) already lists `clients` as
FORBIDDEN, but the LLM still drifts on prompts like AUTH05 ("auth server
policy that restricts token lifetime"). This module is the deterministic
backstop: it walks every `resource "okta_auth_server_policy"` block and
rewrites `clients = [...]` to `client_whitelist = [...]`.

Public API: `sanitize_okta_auth_server_policy(outputs)`.

Pure function. Standard library only. Idempotent. Block-scoped (only
rewrites inside okta_auth_server_policy resource blocks; child rule
resources and other resources stay untouched).
"""

from __future__ import annotations

import re

# Match only the parent policy resource, NOT the child policy_rule resource.
# The regex uses a negative-lookahead so `okta_auth_server_policy_rule`
# blocks do not match.
_POLICY_BLOCK_RE = re.compile(
    r'(resource\s+"okta_auth_server_policy"\s+"[^"]+"\s*\{)([\s\S]*?)(^\})',
    re.MULTILINE,
)

# Match `clients = [...]` on its own line, where the list literal can be
# inline (`["ALL_CLIENTS"]`) or multi-line. The list contents are preserved
# verbatim; only the attribute name changes.
_CLIENTS_LINE_RE = re.compile(
    r'(^[ \t]*)clients(\s*=\s*\[[^\]]*\][^\n]*\n)',
    re.MULTILINE,
)

_HCL_KEYS = ("terraform_okta_hcl",)


def sanitize_okta_auth_server_policy(outputs: dict) -> dict:
    """Rewrite `clients = [...]` to `client_whitelist = [...]` inside every
    `resource "okta_auth_server_policy"` block.

    Closes AUTH05 sampling drift: LLM occasionally emits `clients = [...]`
    despite SECTION B forbidding it. The okta v4.x provider requires
    `client_whitelist` (list of strings) instead.

    Returns a new outputs dict. The input is not mutated. No-op when no
    `okta_auth_server_policy` resource is present.
    """
    result = dict(outputs)
    for key in _HCL_KEYS:
        hcl = result.get(key, "") or ""
        if "okta_auth_server_policy" not in hcl:
            continue
        result[key] = _rewrite_policy_blocks(hcl)
    return result


def _rewrite_policy_blocks(hcl: str) -> str:
    def block_replacement(match: re.Match) -> str:
        opener = match.group(1)
        body = match.group(2)
        closer = match.group(3)
        # The block opener match for `okta_auth_server_policy` will also fire
        # on `okta_auth_server_policy_rule` because the regex is greedy. Skip
        # the rewrite when the opener is the rule resource so child blocks
        # stay untouched.
        if 'okta_auth_server_policy_rule' in opener:
            return opener + body + closer
        new_body = _CLIENTS_LINE_RE.sub(
            lambda m: f"{m.group(1)}client_whitelist{m.group(2)}",
            body,
        )
        return opener + new_body + closer

    return _POLICY_BLOCK_RE.sub(block_replacement, hcl)
