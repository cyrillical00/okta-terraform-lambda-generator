"""Deterministic post-generation sanitizer for okta_event_hook events list.

The LLM frequently drifts on the okta_event_hook `events` set despite the
exhaustive guidance at generator/prompts.py:336-410. Observed misfires:

* "Removed from group X" emitted as `user.lifecycle.delete.initiated`
  (event type does not exist).
* "Users can only be in one of A, B, C" emitted as
  `["user.lifecycle.create", "user.lifecycle.update"]`
  (wrong trigger and `user.lifecycle.update` is hallucinated).
* "Notify when a user's Okta profile is updated" emitted as
  `user.lifecycle.update` (should be `user.account.update_profile`).
* "When a user joins A, remove from B" emitted as
  `["group.user_membership.add", "user.lifecycle.create"]`
  (the `add` is right but `create` is a spurious extra event).

This module is the deterministic backstop. It runs after the LLM 3-pass
refinement loop and rewrites the events list of every `okta_event_hook` block
to the canonical Okta event type derived from the user's prompt language. The
decision tree mirrors the prompt-level guidance but is enforced in code, so
sampling drift is closed unconditionally.

Public API: `sanitize_okta_event_hook_events(outputs, intent)`.

Pure function. Standard library only. Idempotent. No-op when intent lacks a
user_input field or no okta_event_hook blocks are present.
"""

from __future__ import annotations

import re

# Decision tree mirroring generator/prompts.py:336-410 EVENT TYPE SELECTION.
# Order matters: earlier patterns take precedence. Exclusivity and transition
# prompts must match BEFORE the plain "removed from" rule so the trigger
# resolves to `add` (the Lambda body handles the remove) rather than
# `remove`.
_EVENT_TYPE_RULES: list[tuple[re.Pattern, str]] = [
    # 1. Exclusivity / multi-tier / single-membership constraint.
    #    "Users can only be in one of: A, B, C" / "enforce mutual exclusivity"
    (re.compile(
        r"\b(only be in one|one of:|mutual exclusivity|exclusive|can only be in|"
        r"only in one|one .* at a time)\b",
        re.IGNORECASE,
    ), "group.user_membership.add"),
    # 2. Transition with explicit remove side-effect.
    #    "When a user joins A, remove them from B" -> trigger is ADD.
    (re.compile(
        r"\b(joins?|added to|enters?|moves? (in)?to|transitions? (in)?to)\b"
        r"[\s\S]{0,200}\b(removes?|kick|exits?|deletes?)\b",
        re.IGNORECASE,
    ), "group.user_membership.add"),
    # 3. Group removal (no join+remove pattern matched first).
    (re.compile(
        r"\b(removed from|removal from|leaves?|exits?|kicked (out )?(from|of))\b"
        r"[\s\S]{0,80}\bgroup\b",
        re.IGNORECASE,
    ), "group.user_membership.remove"),
    # 4. Plain group add.
    (re.compile(
        r"\b(added to|joins?|becomes? a member of|assigned to)\b"
        r"[\s\S]{0,80}\bgroup\b",
        re.IGNORECASE,
    ), "group.user_membership.add"),
    # 5. Password change / reset / update.
    (re.compile(
        r"\b(password (change|reset|update)|change[sd]? .* password|"
        r"reset[s]? .* password|updates? .* password)\b",
        re.IGNORECASE,
    ), "user.account.update_password"),
    # 6. Profile attribute update.
    (re.compile(
        r"\b(profile (is |attributes? )?(updat|chang)|"
        r"profile attributes? (are |is )?(updat|chang)|"
        r"attribute (is |has )?(updat|chang))",
        re.IGNORECASE,
    ), "user.account.update_profile"),
    # 7. Deactivation / offboarding (literal "user deactivat" / "user offboard"
    #    / "user suspend" — bare "offboard" alone is too greedy because phrases
    #    like "onboarding/offboarding workflow" describe a flow, not a trigger).
    (re.compile(
        r"\b(user (is |account is )?(deactivat|offboard|suspend)|"
        r"account (is )?(deactivat|suspend)|"
        r"deactivat[a-z]* (a |the )?user)",
        re.IGNORECASE,
    ), "user.lifecycle.deactivate"),
    # 8. New user provisioned. Checked BEFORE activation so prompts containing
    #    both "new user is created" and "onboarding workflow" route to .create.
    (re.compile(
        r"\b(new user (is )?(created|provisioned)|user account is created|"
        r"a user is created in (the )?(directory|okta)|user creation event)\b",
        re.IGNORECASE,
    ), "user.lifecycle.create"),
    # 9. Activation / reactivation. Tightened to require user/account context;
    #    bare "onboard" / "onboarding" no longer triggers (it describes a flow,
    #    not a lifecycle event).
    (re.compile(
        r"\b(account (is )?activat|user (is )?activat|reactivat|"
        r"user (is )?onboard|account (is )?onboard)",
        re.IGNORECASE,
    ), "user.lifecycle.activate"),
    # 10. User deletion.
    (re.compile(
        r"\b(user (is )?deleted|account (is )?deleted|user deletion)\b",
        re.IGNORECASE,
    ), "user.lifecycle.delete"),
    # 11. App assignment.
    (re.compile(
        r"\b(app(lication)? (is )?assigned|user assigned to (the )?app)",
        re.IGNORECASE,
    ), "application.user_membership.add"),
]

_EVENT_HOOK_BLOCK_RE = re.compile(
    r'(resource\s+"okta_event_hook"\s+"[^"]+"\s*\{)([\s\S]*?)(^\})',
    re.MULTILINE,
)

# Match `events = [...]` (single or multi-line list, with quoted strings).
_EVENTS_LINE_RE = re.compile(
    r'(^[ \t]*events\s*=\s*\[)([^\]]*)(\])',
    re.MULTILINE,
)

# Map-typed-as-block drift: the v4.x schema declares `auth` and `channel` as
# MAP attributes (e.g. `auth = { type = "OAUTH_TWO_LEGGED", key = "..." }`),
# but the LLM occasionally emits them as bare blocks (`auth { type = ... }`).
# That syntax fails terraform validate with "Unsupported block type". This
# regex matches `<name> {\n ... \n}` inside an event-hook body and rewrites it
# to `<name> = {\n ... \n}`.
_MAP_AS_BLOCK_RE = re.compile(
    r'(^[ \t]*)(auth|channel)\s*\{(\s*\n[\s\S]*?^[ \t]*\})',
    re.MULTILINE,
)

_HCL_KEYS = ("terraform_okta_hcl",)


def sanitize_okta_event_hook_events(outputs: dict, intent: dict) -> dict:
    """Rewrite the events list of every okta_event_hook resource to the canonical event type.

    Args:
        outputs: Generator output dict. Only terraform_okta_hcl is rewritten.
        intent: Intent dict; must contain `user_input` (the raw user prompt
            attached by parse_intent). When user_input is missing, this is
            a no-op so the sanitizer never breaks generators that bypass
            parse_intent.

    Returns:
        A new outputs dict (the input is not mutated). The `events` list of
        every okta_event_hook resource is rewritten to exactly one canonical
        event type derived from the prompt language. When no rule matches,
        the sanitizer is conservative and leaves the events list untouched.
    """
    user_input = (intent or {}).get("user_input", "") or ""
    canonical = _derive_canonical_event(user_input) if user_input.strip() else None

    result = dict(outputs)
    for key in _HCL_KEYS:
        hcl = result.get(key, "") or ""
        if "okta_event_hook" not in hcl:
            continue
        if canonical is not None:
            hcl = _rewrite_event_hook_blocks(hcl, canonical)
        hcl = _rewrite_map_as_block(hcl)
        result[key] = hcl
    return result


def _derive_canonical_event(prompt: str) -> str | None:
    for pattern, event in _EVENT_TYPE_RULES:
        if pattern.search(prompt):
            return event
    return None


def _rewrite_event_hook_blocks(hcl: str, canonical: str) -> str:
    def block_replacement(match: re.Match) -> str:
        opener = match.group(1)
        body = match.group(2)
        closer = match.group(3)
        new_body = _rewrite_events_line(body, canonical)
        return opener + new_body + closer

    return _EVENT_HOOK_BLOCK_RE.sub(block_replacement, hcl)


def _rewrite_events_line(body: str, canonical: str) -> str:
    def events_replacement(match: re.Match) -> str:
        prefix = match.group(1)
        suffix = match.group(3)
        return f'{prefix}"{canonical}"{suffix}'

    return _EVENTS_LINE_RE.sub(events_replacement, body)


def _rewrite_map_as_block(hcl: str) -> str:
    """Convert `auth {...}` / `channel {...}` bare-block syntax inside
    okta_event_hook resources to the map-attribute syntax (`auth = {...}`).

    Only applies inside okta_event_hook resource bodies so the rewrite cannot
    affect other resources where `auth` or `channel` might be legitimate blocks.
    """

    def block_replacement(match: re.Match) -> str:
        opener = match.group(1)
        body = match.group(2)
        closer = match.group(3)

        def map_replacement(inner: re.Match) -> str:
            indent = inner.group(1)
            name = inner.group(2)
            tail = inner.group(3)
            return f"{indent}{name} = {{{tail}"

        new_body = _MAP_AS_BLOCK_RE.sub(map_replacement, body)
        return opener + new_body + closer

    return _EVENT_HOOK_BLOCK_RE.sub(block_replacement, hcl)
