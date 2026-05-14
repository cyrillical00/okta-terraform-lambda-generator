"""Pre-generation detector for enumerated resource lists in user prompts.

When a prompt says "create three groups: A, B, C" the generator should emit
three separate `resource` blocks rather than one block representing the first
group only (the observed JF10 / COMP02 failure mode under LLM sampling
pressure). This module extracts the enumerated instances from the prompt and
returns them in a structured list that `parse_intent` attaches to the intent
dict as `intent["instances"]`. Downstream the generator user-prompt template
surfaces the list, and a post-generation sanitizer (see
`multi_object_sanitizer.py`) clones the emitted block if the LLM still drifts.

Detection covers three prompt shapes:

1. **Count + colon list**: "Create three groups: A, B, C" /
   "two scopes: read:data, write:data". The numeric count must match the
   parsed list length, else the detector falls back.
2. **Trailing comma list with `and`**: "...assign three groups: HR, Finance,
   and Executives".
3. **"For/called/named" + list**: "Create groups for HR, Finance, and
   Executives" / "named A, B, C".

Returns None when no pattern matches or fewer than 2 names are extracted.

Public API: `detect_instances(prompt)`.

Pure function. Standard library only. Idempotent. Conservative — when a
prompt is ambiguous it returns None rather than a wrong list.
"""

from __future__ import annotations

import re

_NUMERIC_WORDS: dict[str, int] = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# Pattern 1: "<count> ... <kind>: <comma-separated list>".
# Capture group 1 = numeric word; group 2 = list tail.
# The kind word (group/policy/scope/etc.) is enforced via the (?:...) cluster
# so we don't accidentally match unrelated count+colon prose.
_KIND_WORDS = r"groups?|policies|policy|rules?|apps?|servers?|functions?|topics?|zones?|scopes?|claims?|assignments?|attributes?|payloads?|profiles?"

_COUNT_COLON_RE = re.compile(
    r"\b(two|three|four|five|six|seven|eight|nine|ten)\s+(?:\w+\s+){0,3}"
    r"(?:" + _KIND_WORDS + r")\b"
    # Optional prose tail between kind and colon: "on the X auth server",
    # "for the Y API", etc. Bounded to avoid runaway matching.
    r"(?:[^.:\n]{0,80}?)?"
    r"\s*[:\-]\s*([^.\n]+)",
    re.IGNORECASE,
)

# Pattern 2: "for|called|named|labelled|labeled <comma-list>".
_FOR_NAMED_RE = re.compile(
    r"\b(?:for|called|named|labell?ed)\s+([A-Z][\w\- ]+(?:,\s*[A-Z][\w\- ]+){1,9}(?:,?\s+and\s+[A-Z][\w\- ]+)?)",
    re.IGNORECASE,
)

# Pattern 3: bare trailing comma-list with `and` separator.
# Stricter to avoid eating sentence fragments — needs at least 3 items.
_BARE_LIST_RE = re.compile(
    r":\s*([\w][\w \-:]{0,40},\s*[\w][\w \-:]{0,40}(?:,\s*[\w][\w \-:]{0,40}){0,8})\s*(?:\.|$)",
)


def detect_instances(prompt: str) -> list[dict] | None:
    """Return a list of `{"name": ...}` dicts when the prompt enumerates
    multiple resources, else None.

    Conservative: returns None on ambiguous prompts, single-object prompts,
    or when the parsed list has fewer than 2 valid items. Caller (parse_intent)
    attaches the list to intent["instances"] only when non-None.
    """
    if not prompt or len(prompt.strip()) < 8:
        return None

    candidate_lists: list[tuple[str, int | None]] = []

    for m in _COUNT_COLON_RE.finditer(prompt):
        count = _NUMERIC_WORDS.get(m.group(1).lower())
        candidate_lists.append((m.group(2), count))

    for m in _FOR_NAMED_RE.finditer(prompt):
        candidate_lists.append((m.group(1), None))

    if not candidate_lists:
        for m in _BARE_LIST_RE.finditer(prompt):
            candidate_lists.append((m.group(1), None))

    for tail, expected_count in candidate_lists:
        names = _split_list(tail)
        if expected_count is not None and len(names) != expected_count:
            # The numeric word and the list length disagree. Conservative:
            # skip this candidate. Could be "three scopes: read, write" where
            # the third scope is implied. Caller will fall back to LLM judgment.
            continue
        if len(names) >= 2:
            return [{"name": n} for n in names]

    return None


def _split_list(tail: str) -> list[str]:
    """Split a list tail like 'HR, Finance, and Executives' into clean names.

    Handles the Oxford comma, the `and` separator, trailing punctuation, and
    optional inner whitespace. Names that contain colons or slashes (e.g.
    `read:data`) are preserved as-is.
    """
    # Strip a trailing period or terminator and trim outer whitespace.
    tail = tail.strip().rstrip(".")
    # Normalise `, and ` / `, or ` and ` and ` / ` or ` to a single comma
    # separator. Both `and` (inclusive) and `or` (alternative) appear in
    # enumeration tails like "Free, Pro, or Enterprise". Without this the
    # third item would carry the leading "or " which corrupts the slug.
    tail = re.sub(r",\s*(?:and|or)\s+", ", ", tail, flags=re.IGNORECASE)
    tail = re.sub(r"\s+(?:and|or)\s+", ", ", tail, flags=re.IGNORECASE)
    parts = [p.strip() for p in tail.split(",")]
    # Drop empties and any parts that look like prose tails ("based on their
    # department", "for downstream provisioning"). A name is conservatively
    # a short token of <= 40 chars containing word chars and at most a few
    # spaces/dashes/colons.
    names: list[str] = []
    for p in parts:
        if not p:
            continue
        if len(p) > 40:
            continue
        if not re.match(r"^[\w][\w \-:/]*$", p):
            continue
        names.append(p)
    return names
