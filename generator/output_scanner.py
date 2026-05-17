"""Post-generation secret-shape scanner. Symmetric to redact.py:
redact.py scrubs inbound prompts before they reach Anthropic;
output_scanner.py scrubs outbound generated code (HCL / YAML / Python)
before it can be pushed to GitHub or downloaded.

The LLM occasionally hallucinates secret-shaped strings inline in
generated code (e.g. an example RSA key in a JAMF script body, or a
fake AWS access key in a comment). Live env-context can also smuggle
real values into the parser-supplied context section that the
generator references. This scanner catches both classes.

Public API:
  - `scan_outputs_for_secrets(outputs) -> list[dict]`, where each dict
    carries `key`, `category`, `line`, `snippet`. The `snippet` always
    has the matched bytes replaced with `<...>` so audit logs and UI
    panels never leak the underlying secret.

Implementation notes:
  - The regex set is imported directly from `redact._PATTERNS` rather
    than duplicated; this guarantees both sides of the prompt/output
    boundary use the same coverage. Adding a category in `redact.py`
    automatically extends the output scanner.
  - The IPv4 allowlist (loopback, 0.0.0.0, well-known DNS) is honored
    here too, mirroring the redact-side behaviour. Instructional
    examples shouldn't flag.
  - Credit-card detection is intentionally skipped on outputs. The
    redact module gates it behind a Luhn check; even with that, the
    false-positive rate inside generated Terraform (resource IDs,
    epoch timestamps, base64 chunks) is too high for a hard gate.
  - The scanner is read-only. It never mutates `outputs`; it returns
    findings and lets the caller decide whether to block.
"""

from __future__ import annotations

import re

from redact import _PATTERNS, _IPV4_ALLOWLIST, _IPV4_RE


# Outputs the scanner inspects. Anything not in this tuple is ignored;
# this is the same set of keys downstream renderers / GitHub push code
# treats as user-visible artifacts. Internal bookkeeping keys (e.g.
# `_secret_scan_findings` itself) start with an underscore and are
# never scanned.
_OUTPUT_KEYS = (
    "terraform_okta_hcl",
    "terraform_lambda_hcl",
    "terraform_gcp_hcl",
    "terraform_jamf_hcl",
    "fleet_gitops_yaml",
    "terraform_fleet_hcl",
    "terraform_snowflake_hcl",
    "lambda_python",
    "cloud_function_python",
    "optional_tf",
)


def _line_and_snippet(text: str, match_start: int, match_end: int) -> tuple[int, str]:
    """Return (1-based line number, redacted snippet) for a regex match
    against `text`. The snippet is the full source line with the matched
    bytes replaced by `<...>` so audit logs never quote the secret."""
    line_start = text.rfind("\n", 0, match_start) + 1
    line_end = text.find("\n", match_end)
    if line_end == -1:
        line_end = len(text)
    line_no = text.count("\n", 0, match_start) + 1
    raw_line = text[line_start:line_end]
    # Map the match position into the line-local span and overwrite.
    local_start = match_start - line_start
    local_end = match_end - line_start
    redacted = raw_line[:local_start] + "<...>" + raw_line[local_end:]
    # For multi-line matches (PEM blocks, GCP SA JSON) the raw match
    # spans multiple newlines, which means `local_end` can point past
    # the first line. Collapse the span by truncating at the first
    # newline on either side so the snippet stays a single line that
    # is safe to display.
    redacted = redacted.split("\n", 1)[0]
    # Trim leading whitespace so the snippet renders tidily in audit
    # logs and the UI; line number already gives the exact location.
    return line_no, redacted.strip()


def _scan_one_value(value: str) -> list[tuple[str, int, str]]:
    """Run every redact pattern (plus the IPv4 allowlist-aware scan)
    against `value`. Returns a list of (category, line_no, snippet)
    tuples. Empty when no matches."""
    findings: list[tuple[str, int, str]] = []
    if not value:
        return findings

    # Static patterns from redact._PATTERNS. Each entry is
    # (label, compiled_regex, replacement). We only need the first two.
    for label, regex, _replacement in _PATTERNS:
        for m in regex.finditer(value):
            line_no, snippet = _line_and_snippet(value, m.start(), m.end())
            findings.append((label, line_no, snippet))

    # IPv4: mirror redact._redact_ipv4 by applying the well-known-public
    # allowlist so loopback / 0.0.0.0 / 1.1.1.1 / 8.8.8.8 etc. don't
    # flag in instructional examples.
    for m in _IPV4_RE.finditer(value):
        if m.group(0) in _IPV4_ALLOWLIST:
            continue
        line_no, snippet = _line_and_snippet(value, m.start(), m.end())
        findings.append(("ipv4", line_no, snippet))

    return findings


def scan_outputs_for_secrets(outputs: dict) -> list[dict]:
    """Scan every user-visible output for secret-shaped substrings.

    Returns a list of finding dicts, one per match:
        {
            "key":      "terraform_okta_hcl",
            "category": "private_key",
            "line":     42,
            "snippet":  "private_key = <...>",
        }

    Empty list when no findings. Order is stable: outputs are scanned
    in `_OUTPUT_KEYS` order, and within each output, matches are
    sorted by line number.
    """
    findings: list[dict] = []
    if not isinstance(outputs, dict):
        return findings
    for key in _OUTPUT_KEYS:
        value = outputs.get(key)
        if not isinstance(value, str) or not value:
            continue
        raw = _scan_one_value(value)
        # Stable sort by line number so multi-finding outputs render
        # top-to-bottom in the UI and audit log.
        raw.sort(key=lambda t: (t[1], t[0]))
        for category, line_no, snippet in raw:
            findings.append({
                "key": key,
                "category": category,
                "line": line_no,
                "snippet": snippet,
            })
    return findings


def format_findings(findings: list[dict]) -> str:
    """Render findings as a human-readable summary block. Empty string
    when there are no findings. Used by `qa_runner.py` and `app.py` to
    surface the same audit line in tests and UI."""
    if not findings:
        return ""
    lines = []
    for f in findings:
        lines.append(
            f"Secret-shape '{f['category']}' detected in {f['key']} "
            f"line {f['line']}: {f['snippet']}"
        )
    return "\n".join(lines)
