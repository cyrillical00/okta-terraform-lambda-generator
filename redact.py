"""PII / secret redaction for prompts before they leave Streamlit for Anthropic.

Strategy:
- Strip patterns that are personally identifiable or are credentials.
- Preserve patterns that are infrastructure context the LLM needs to do its
  job (IPs, hostnames, URLs, GCP project IDs, Okta org names).
- Replace each match with a typed placeholder so the model still gets
  "there is an email here" without seeing the literal address.

Side-effect free at module load. Pure-regex; no I/O, no third-party deps.

Public API:
  - `redact(text) -> (cleaned, summary)` — summary is dict[label -> count]
  - `format_summary(summary) -> str` — human-readable for the UI notice
"""

from __future__ import annotations

import re

# Patterns: (label, regex, placeholder).
# Order matters: longer / more-specific patterns first so they win the match.
_PATTERNS = [
    # Anthropic / OpenAI / Stripe / GitHub PATs first — these are the most
    # dangerous if they leak into a prompt.
    ("api_key_anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), "[ANTHROPIC_KEY_REDACTED]"),
    ("api_key_openai",    re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),         "[OPENAI_KEY_REDACTED]"),
    ("api_key_stripe",    re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b"), "[STRIPE_KEY_REDACTED]"),
    ("github_pat",        re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),        "[GITHUB_PAT_REDACTED]"),
    ("github_pat_fine",   re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),"[GITHUB_PAT_REDACTED]"),
    # AWS access key ID — known prefix list per AWS docs (AKIA, ASIA, etc.).
    ("aws_access_key",    re.compile(r"\b(?:AKIA|ASIA|AROA|AIDA|ANPA|ANVA|AGPA)[A-Z0-9]{16}\b"), "[AWS_ACCESS_KEY_REDACTED]"),
    # JWT (3-part base64url separated by dots, each segment >= 8 chars).
    ("jwt",               re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[JWT_REDACTED]"),
    # SSN: NNN-NN-NNNN (avoid matching long digit runs without dashes — too noisy).
    ("ssn",               re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),           "[SSN_REDACTED]"),
    # Credit-card-ish: 13-19 digits, optionally separated by spaces or dashes.
    # Validate with Luhn so common fake numbers (1234-5678-...) don't false-positive.
    # Implemented as a function check below.
    # Phone: US-style with dashes / spaces / parens. Conservative — only matches
    # patterns that look like phone numbers, not bare 10-digit sequences.
    ("phone_us",          re.compile(
        r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"
    ), "[PHONE_REDACTED]"),
    # Email — last because it's the broadest pattern.
    ("email",             re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[EMAIL_REDACTED]"),
]


def _luhn_ok(digits: str) -> bool:
    """Standard Luhn checksum over a string of digits."""
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


_CC_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _redact_cc(text: str, summary: dict) -> str:
    """Find credit-card-shaped sequences, validate via Luhn, replace if valid."""
    def repl(m: re.Match) -> str:
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        if _luhn_ok(digits):
            summary["credit_card"] = summary.get("credit_card", 0) + 1
            return "[CC_REDACTED]"
        return raw
    return _CC_RE.sub(repl, text)


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Redact PII / secrets from `text`. Returns (cleaned, summary) where
    summary maps label -> count.

    Patterns NOT redacted on purpose (load-bearing infrastructure context):
      IP addresses, hostnames, full URLs, GCP project IDs, Okta org names,
      SAML entity IDs, ARNs, role names. The model needs these to generate
      correct Terraform.
    """
    if not text:
        return text or "", {}
    summary: dict[str, int] = {}
    out = text
    for label, regex, placeholder in _PATTERNS:
        def repl(m, lbl=label, ph=placeholder):
            summary[lbl] = summary.get(lbl, 0) + 1
            return ph
        out = regex.sub(repl, out)
    out = _redact_cc(out, summary)
    return out, summary


_LABEL_DISPLAY = {
    "email": "email",
    "phone_us": "phone",
    "ssn": "SSN",
    "credit_card": "credit card",
    "api_key_anthropic": "Anthropic key",
    "api_key_openai": "OpenAI key",
    "api_key_stripe": "Stripe key",
    "github_pat": "GitHub PAT",
    "github_pat_fine": "GitHub PAT",
    "aws_access_key": "AWS access key",
    "jwt": "JWT",
}


def format_summary(summary: dict[str, int]) -> str:
    """Human-readable summary for the UI notice. Empty when nothing redacted."""
    if not summary:
        return ""
    parts = []
    for label, count in summary.items():
        name = _LABEL_DISPLAY.get(label, label)
        plural = "s" if count != 1 else ""
        parts.append(f"{count} {name}{plural}")
    return ", ".join(parts)
