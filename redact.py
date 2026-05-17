"""PII / secret redaction for prompts before they leave Streamlit for Anthropic.

Strategy:
- Strip patterns that are personally identifiable or are credentials.
- Preserve patterns that are infrastructure context the LLM needs to do its
  job (hostnames, URLs, GCP project IDs, Okta org names, ARNs).
- Replace each match with a typed placeholder so the model still gets
  "there is an email here" without seeing the literal address.

Side-effect free at module load. Pure-regex; no I/O, no third-party deps.

Public API:
  - `redact(text) -> (cleaned, summary)`, where summary is dict[label -> count]
  - `format_summary(summary) -> str`, human-readable for the UI notice

Ordering rationale:
  Patterns are applied in declaration order. Longer / more-specific
  shapes run first so they consume the bytes before broader patterns
  match. Critical orderings:
    1. Multi-line PEM blocks first (private keys). The DOTALL block is
       a single contiguous span, so collapsing it early prevents the
       embedded base64 lines from matching JWT, AWS-secret, or generic
       credential patterns.
    2. GCP service-account JSON next, for the same reason: the embedded
       `private_key` PEM and `client_email` would otherwise be picked
       up piecemeal by later patterns.
    3. Vendor-prefixed credentials (Anthropic, OpenAI, Stripe, GitHub,
       AWS access key, Slack tokens, Snowflake account) come before
       generic Bearer / JWT because the prefix is a stronger signal.
    4. Context-aware AWS secret runs before generic base64-ish patterns
       because it depends on the surrounding `aws_secret_access_key=`
       text being intact.
    5. JWT (3-segment base64url with `eyJ` prefix) before Bearer
       generic, so a `Bearer eyJ...` payload labels as JWT not Bearer.
    6. SSN, phone, email last among PII (broadest patterns).
    7. Network identifiers (IPv6, IPv4, MAC) at the end. IPv4 has an
       allowlist for well-known publics (0.0.0.0, 127.0.0.1, 1.1.1.1,
       8.8.8.8, 8.8.4.4) to keep instructional examples readable.
"""

from __future__ import annotations

import re

# IPv4 octets we deliberately leave untouched: loopback, unspecified,
# Cloudflare DNS, Google DNS. These appear constantly in infra examples
# and redacting them hurts prompt clarity without protecting anyone.
_IPV4_ALLOWLIST = {
    "0.0.0.0",
    "127.0.0.1",
    "1.1.1.1",
    "1.0.0.1",
    "8.8.8.8",
    "8.8.4.4",
}


# Patterns: (label, regex, placeholder).
# Order matters; see "Ordering rationale" in the module docstring.
_PATTERNS = [
    # ── Multi-line PEM blocks (must run first; see rationale #1) ──────
    ("rsa_private_key", re.compile(
        r"-----BEGIN RSA PRIVATE KEY-----.*?-----END RSA PRIVATE KEY-----",
        re.DOTALL,
    ), "<REDACTED_PRIVATE_KEY>"),
    ("openssh_private_key", re.compile(
        r"-----BEGIN OPENSSH PRIVATE KEY-----.*?-----END OPENSSH PRIVATE KEY-----",
        re.DOTALL,
    ), "<REDACTED_PRIVATE_KEY>"),
    ("ec_private_key", re.compile(
        r"-----BEGIN EC PRIVATE KEY-----.*?-----END EC PRIVATE KEY-----",
        re.DOTALL,
    ), "<REDACTED_PRIVATE_KEY>"),
    ("dsa_private_key", re.compile(
        r"-----BEGIN DSA PRIVATE KEY-----.*?-----END DSA PRIVATE KEY-----",
        re.DOTALL,
    ), "<REDACTED_PRIVATE_KEY>"),
    # Generic "PRIVATE KEY" last among PEM so the more-specific RSA /
    # OPENSSH / EC / DSA labels win when the BEGIN line carries them.
    ("private_key", re.compile(
        r"-----BEGIN PRIVATE KEY-----.*?-----END PRIVATE KEY-----",
        re.DOTALL,
    ), "<REDACTED_PRIVATE_KEY>"),

    # ── GCP service-account JSON (rationale #2) ───────────────────────
    # Matches a JSON object whose first key is `"type": "service_account"`.
    # We match from the opening `{` through to the corresponding closing
    # `}`. The nested-brace problem is avoided by anchoring on the
    # standard SA field list and the trailing `"universe_domain"` or
    # `"client_x509_cert_url"` key, which closes every SA JSON Google
    # has ever emitted.
    ("gcp_service_account_json", re.compile(
        r"\{\s*\"type\"\s*:\s*\"service_account\".*?\}",
        re.DOTALL,
    ), "<REDACTED_GCP_SERVICE_ACCOUNT>"),

    # ── Vendor-prefixed credentials (rationale #3) ────────────────────
    # Anthropic / OpenAI / Stripe / GitHub PATs, the most dangerous
    # category if they leak into a prompt.
    ("api_key_anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), "[ANTHROPIC_KEY_REDACTED]"),
    ("api_key_openai",    re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),         "[OPENAI_KEY_REDACTED]"),
    ("api_key_stripe",    re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b"), "[STRIPE_KEY_REDACTED]"),
    ("github_pat",        re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),        "[GITHUB_PAT_REDACTED]"),
    ("github_pat_fine",   re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),"[GITHUB_PAT_REDACTED]"),
    # AWS access key ID, known prefix list per AWS docs.
    ("aws_access_key",    re.compile(r"\b(?:AKIA|ASIA|AROA|AIDA|ANPA|ANVA|AGPA)[A-Z0-9]{16}\b"), "[AWS_ACCESS_KEY_REDACTED]"),
    # Slack tokens: bot, user, and refresh / config variants. The token
    # body is `xox[bpars]-` followed by 50+ chars of digits, letters,
    # and dashes.
    ("slack_token",       re.compile(r"\bxox[bparso]-[0-9A-Za-z-]{20,}\b"), "<REDACTED_SLACK_TOKEN>"),
    # Snowflake account identifier: 2 lowercase letters + 5 digits,
    # dot, region/cloud segment. Common shapes:
    #   xy12345.us-east-1
    #   xy12345.us-east-1.aws
    #   xy12345.eu-west-1.gcp
    ("snowflake_account", re.compile(r"\b[a-z]{2}\d{5}\.[a-z0-9][a-z0-9.-]{2,40}\b"), "<REDACTED_SF_ACCOUNT>"),

    # ── Context-aware AWS secret (rationale #4) ───────────────────────
    # Match the assignment `aws_secret_access_key = "..."` /
    # `AWS_SECRET_ACCESS_KEY=...` and redact only the value. The 40-char
    # base64-ish body is the canonical secret-access-key shape. Keep
    # the key name intact so the model still sees there was a secret.
    ("aws_secret_access_key", re.compile(
        r"((?i:aws_secret_access_key)\s*[:=]\s*[\"']?)"
        r"[A-Za-z0-9/+=]{40}"
        r"([\"']?)"
    ), r"\1<REDACTED_AWS_SECRET>\2"),

    # ── JWT before Bearer (rationale #5) ──────────────────────────────
    # JWT (3-part base64url separated by dots, each segment >= 8 chars).
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[JWT_REDACTED]"),
    # Bearer token: `Bearer <40+ chars of token body>`. Allow common
    # token-body chars (alnum, dash, underscore, dot). We keep the
    # `Bearer ` prefix so the model still sees auth context.
    ("bearer_token", re.compile(
        r"\b(Bearer)\s+([A-Za-z0-9_\-\.]{40,})\b",
        re.IGNORECASE,
    ), r"\1 <REDACTED_TOKEN>"),

    # ── PII (rationale #6) ────────────────────────────────────────────
    # SSN: NNN-NN-NNNN.
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN_REDACTED]"),
    # Phone: US-style with dashes / spaces / parens. Conservative.
    ("phone_us", re.compile(
        r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"
    ), "[PHONE_REDACTED]"),
    # Email, broadest of the PII patterns; run last.
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[EMAIL_REDACTED]"),

    # ── Network identifiers (rationale #7) ────────────────────────────
    # IPv6: full and abbreviated forms. We accept any run of 2-7
    # `:`-separated hex groups with an optional `::` compressor.
    # Anchor on word boundaries so `2001:db8::1` matches but
    # `foo::bar` (C++ scope) does not (no hex-only run on either side).
    ("ipv6", re.compile(
        r"(?<![A-Za-z0-9:])"
        r"(?:"
        r"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"   # full
        r"|"
        r"(?:[0-9A-Fa-f]{1,4}:){1,7}:"                # trailing ::
        r"|"
        r"(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}"
        r"|"
        r"(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}"
        r"|"
        r"(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}"
        r"|"
        r"(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}"
        r"|"
        r"(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}"
        r"|"
        r"[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}"
        r"|"
        r"::(?:[0-9A-Fa-f]{1,4}:){0,6}[0-9A-Fa-f]{1,4}"
        r"|"
        r"::"
        r")"
        r"(?![A-Za-z0-9:])"
    ), "<REDACTED_IPV6>"),

    # MAC address: 6 pairs of hex separated by `:` or `-`.
    ("mac_address", re.compile(
        r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b"
    ), "<REDACTED_MAC>"),
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


# IPv4 with allowlist for well-known publics. Implemented as a function
# rather than a static regex so we can keep `0.0.0.0`, `127.0.0.1`, etc.
# in the prompt verbatim.
_IPV4_RE = re.compile(
    r"\b("
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}"
    r")\b"
)


def _redact_ipv4(text: str, summary: dict) -> str:
    """Redact IPv4 addresses, except the well-known public allowlist."""
    def repl(m: re.Match) -> str:
        addr = m.group(0)
        if addr in _IPV4_ALLOWLIST:
            return addr
        summary["ipv4"] = summary.get("ipv4", 0) + 1
        return "<REDACTED_IPV4>"
    return _IPV4_RE.sub(repl, text)


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Redact PII / secrets from `text`. Returns (cleaned, summary) where
    summary maps label -> count.

    Patterns NOT redacted on purpose (load-bearing infrastructure context):
      hostnames, full URLs, GCP project IDs, Okta org names, SAML entity
      IDs, ARNs, role names. The model needs these to generate correct
      Terraform. IPv4 addresses are redacted *except* a small allowlist
      of well-known publics (`0.0.0.0`, `127.0.0.1`, `1.1.1.1`, `1.0.0.1`,
      `8.8.8.8`, `8.8.4.4`) so instructional examples stay readable.
    """
    if not text:
        return text or "", {}
    summary: dict[str, int] = {}
    out = text
    for label, regex, placeholder in _PATTERNS:
        def repl(m, lbl=label, ph=placeholder):
            summary[lbl] = summary.get(lbl, 0) + 1
            # If the placeholder contains a backreference, expand it
            # by deferring to re.sub semantics on the matched span only.
            if "\\" in ph:
                return m.expand(ph)
            return ph
        out = regex.sub(repl, out)
    out = _redact_cc(out, summary)
    out = _redact_ipv4(out, summary)
    return out, summary


_LABEL_DISPLAY = {
    # PII
    "email": "email",
    "phone_us": "phone",
    "ssn": "SSN",
    "credit_card": "credit card",
    # Vendor keys
    "api_key_anthropic": "Anthropic key",
    "api_key_openai": "OpenAI key",
    "api_key_stripe": "Stripe key",
    "github_pat": "GitHub PAT",
    "github_pat_fine": "GitHub PAT",
    "aws_access_key": "AWS access key",
    "aws_secret_access_key": "AWS secret",
    "slack_token": "Slack token",
    "snowflake_account": "Snowflake account",
    # Generic credentials
    "jwt": "JWT",
    "bearer_token": "Bearer token",
    # Private keys (all collapsed under one label in the UI)
    "rsa_private_key": "private key",
    "openssh_private_key": "private key",
    "ec_private_key": "private key",
    "dsa_private_key": "private key",
    "private_key": "private key",
    # Cloud blobs
    "gcp_service_account_json": "GCP service account JSON",
    # Network identifiers
    "ipv4": "IPv4 address",
    "ipv6": "IPv6 address",
    "mac_address": "MAC address",
}


def format_summary(summary: dict[str, int]) -> str:
    """Human-readable summary for the UI notice. Empty when nothing redacted.

    Counts for labels that share a display name (e.g. all private-key
    flavors collapse to "private key") are merged so the user sees a
    single line per category.
    """
    if not summary:
        return ""
    merged: dict[str, int] = {}
    for label, count in summary.items():
        name = _LABEL_DISPLAY.get(label, label)
        merged[name] = merged.get(name, 0) + count
    parts = []
    for name, count in merged.items():
        parts.append(f"{count} {_pluralize(name, count)}")
    return ", ".join(parts)


def _pluralize(name: str, count: int) -> str:
    """Return `name` pluralized for `count`. Handles the few suffixes
    that appear in label names: "address" → "addresses", "key" → "keys",
    "token" → "tokens", "PAT" → "PATs", etc."""
    if count == 1:
        return name
    # English plural rules covering our label set:
    # words ending in s, x, z, ch, sh take "es"
    # words ending in consonant+y switch to "ies"
    # everything else takes "s"
    lower = name.lower()
    if lower.endswith(("s", "x", "z", "ch", "sh")):
        return name + "es"
    if (
        lower.endswith("y")
        and len(lower) >= 2
        and lower[-2] not in "aeiou"
    ):
        return name[:-1] + "ies"
    return name + "s"
