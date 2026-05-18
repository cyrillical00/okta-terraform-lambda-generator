"""Deterministic post-generation sanitizer for google_cloudfunctions2_function
Pub/Sub trigger shape.

The hashicorp/google v6.x provider exposes the function-trigger block as
`event_trigger { ... }`. The bare block name `trigger { ... }` does NOT
exist in the v6 schema and fails `terraform validate` with "Unsupported
block type". Inside `event_trigger`, the topic reference attribute is
`pubsub_topic`, not `topic_name`.

SECTION C2 of prompts.py (line 655-661) already teaches the canonical
`event_trigger` shape, but the LLM occasionally drifts on GCP02-style
prompts ("Pub/Sub topic ... Cloud Function subscriber"). This module is
the deterministic backstop:

1. Rewrites `trigger { ... }` to `event_trigger { ... }` inside every
   `resource "google_cloudfunctions2_function"` block.
2. Rewrites `topic_name = ...` to `pubsub_topic = ...` inside every
   `event_trigger {}` block (after the trigger->event_trigger rewrite).
3. Auto-fills the two optional but commonly-required fields when missing
   from an `event_trigger {}` that targets a Pub/Sub topic:
   - `trigger_region = var.gcp_region`
   - `retry_policy   = "RETRY_POLICY_RETRY"`

Public API: `sanitize_gcp_cloudfunctions2_trigger(outputs)`.

Pure function. Standard library only. Idempotent. Block-scoped (only
rewrites inside google_cloudfunctions2_function resource blocks).
"""

from __future__ import annotations

import re

_FUNCTION_BLOCK_RE = re.compile(
    r'(resource\s+"google_cloudfunctions2_function"\s+"[^"]+"\s*\{)([\s\S]*?)(^\})',
    re.MULTILINE,
)

# Match a bare `trigger { ... }` block. The negative lookbehind avoids
# matching `event_trigger {`, `http_trigger {`, or any other `*trigger {`.
# The body uses a balanced-brace pattern up to the matching `^  }` line so a
# nested `oidc_token {}` etc. would not break parsing (Pub/Sub triggers do
# not have nested blocks in the v6 schema, so a flat scan is sufficient).
_TRIGGER_BLOCK_RE = re.compile(
    r'(^[ \t]*)trigger(\s*\{[\s\S]*?^\1\})',
    re.MULTILINE,
)

# Match `topic_name = ...` inside an event_trigger block; the line is
# rewritten to `pubsub_topic = ...`. The value (whether a literal string or
# a `google_pubsub_topic.handler.id` reference) is preserved verbatim.
_TOPIC_NAME_LINE_RE = re.compile(
    r'(^[ \t]*)topic_name(\s*=\s*[^\n]+\n)',
    re.MULTILINE,
)

# Match an `event_trigger {}` block scoped to a function body. Used to fill
# missing required-by-convention fields (trigger_region, retry_policy).
_EVENT_TRIGGER_BLOCK_RE = re.compile(
    r'(^[ \t]*)event_trigger(\s*\{)([\s\S]*?)(^\1\})',
    re.MULTILINE,
)

_HCL_KEYS = ("terraform_gcp_hcl",)


def sanitize_gcp_cloudfunctions2_trigger(outputs: dict) -> dict:
    """Rewrite Pub/Sub trigger drift inside google_cloudfunctions2_function
    resource blocks.

    Closes GCP02 sampling drift: LLM occasionally emits `trigger { ... }`
    (wrong block name) or `topic_name = ...` (wrong attribute name) on a
    Pub/Sub-triggered Cloud Function.

    Returns a new outputs dict. The input is not mutated. No-op when no
    `google_cloudfunctions2_function` resource is present.
    """
    result = dict(outputs)
    for key in _HCL_KEYS:
        hcl = result.get(key, "") or ""
        if "google_cloudfunctions2_function" not in hcl:
            continue
        hcl = _rewrite_function_blocks(hcl)
        result[key] = hcl
    return result


def _rewrite_function_blocks(hcl: str) -> str:
    def block_replacement(match: re.Match) -> str:
        opener = match.group(1)
        body = match.group(2)
        closer = match.group(3)

        # Step 1: bare `trigger {}` -> `event_trigger {}`.
        body = _TRIGGER_BLOCK_RE.sub(
            lambda m: f"{m.group(1)}event_trigger{m.group(2)}",
            body,
        )

        # Step 2: inside any `event_trigger {}`, rename `topic_name` to
        # `pubsub_topic` and fill missing trigger_region / retry_policy.
        body = _EVENT_TRIGGER_BLOCK_RE.sub(_fix_event_trigger_body, body)

        return opener + body + closer

    return _FUNCTION_BLOCK_RE.sub(block_replacement, hcl)


def _fix_event_trigger_body(match: re.Match) -> str:
    indent = match.group(1)
    opener = match.group(2)
    inner = match.group(3)
    closer_indent_brace = match.group(4)

    # Topic-name attribute rewrite.
    inner = _TOPIC_NAME_LINE_RE.sub(
        lambda m: f"{m.group(1)}pubsub_topic{m.group(2)}",
        inner,
    )

    # Only auto-fill defaults when this looks like a Pub/Sub event_trigger.
    # Detection: presence of `pubsub_topic` attribute or the Pub/Sub
    # `event_type` string. This keeps GCS / Eventarc triggers untouched.
    is_pubsub = (
        "pubsub_topic" in inner
        or "google.cloud.pubsub.topic.v1.messagePublished" in inner
    )

    if is_pubsub:
        inner_indent = indent + "  "
        if "trigger_region" not in inner:
            inner = inner.rstrip("\n") + (
                f"\n{inner_indent}trigger_region = var.gcp_region"
                f"  # auto-filled by Phase 20 sanitizer\n"
            )
        if "retry_policy" not in inner:
            inner = inner.rstrip("\n") + (
                f'\n{inner_indent}retry_policy   = "RETRY_POLICY_RETRY"'
                f"  # auto-filled by Phase 20 sanitizer\n"
            )

    return f"{indent}event_trigger{opener}{inner}{closer_indent_brace}"
