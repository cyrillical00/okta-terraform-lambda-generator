"""Tests for `redact` module.

Standalone-runnable: `python tests/test_redact.py`.
pytest-compatible: `pytest tests/test_redact.py -q`.

Coverage:
- Pre-existing patterns (email, phone, SSN, credit card, Anthropic /
  OpenAI / Stripe keys, GitHub PATs, AWS access key ID, JWT) get a
  light smoke test to confirm Phase 17b did not regress them.
- New Phase 17b patterns each get a positive (the pattern is redacted)
  and a negative (a lookalike non-credential is not redacted) test.
- Performance smoke: 10KB prompt redacts in under 100ms.
"""

from __future__ import annotations

import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import redact


# ── Pre-existing patterns: smoke tests so we don't regress them ──────


def test_email_redacted():
    out, summary = redact.redact("Contact alice@example.com for details.")
    assert "[EMAIL_REDACTED]" in out
    assert "alice@example.com" not in out
    assert summary.get("email") == 1


def test_phone_redacted():
    out, summary = redact.redact("Call me at 555-123-4567 tomorrow.")
    assert "[PHONE_REDACTED]" in out
    assert summary.get("phone_us") == 1


def test_ssn_redacted():
    out, summary = redact.redact("My SSN is 123-45-6789.")
    assert "[SSN_REDACTED]" in out
    assert summary.get("ssn") == 1


def test_credit_card_luhn_validated():
    # 4111 1111 1111 1111 is the canonical test card (passes Luhn).
    out, summary = redact.redact("Card: 4111 1111 1111 1111")
    assert "[CC_REDACTED]" in out
    assert summary.get("credit_card") == 1


def test_credit_card_invalid_luhn_not_redacted():
    # 1234567890123456 is 16 digits but does not pass Luhn.
    out, summary = redact.redact("Random number: 1234567890123456")
    assert "[CC_REDACTED]" not in out
    assert "credit_card" not in summary


def test_anthropic_key_redacted():
    key = "sk-ant-api03-" + "A" * 93
    out, summary = redact.redact(f"export ANTHROPIC_API_KEY={key}")
    assert "[ANTHROPIC_KEY_REDACTED]" in out
    assert key not in out
    assert summary.get("api_key_anthropic") == 1


def test_github_pat_classic_redacted():
    pat = "ghp_" + "a" * 36
    out, summary = redact.redact(f"token={pat}")
    assert "[GITHUB_PAT_REDACTED]" in out
    assert summary.get("github_pat") == 1


def test_aws_access_key_id_redacted():
    out, summary = redact.redact("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE")
    assert "[AWS_ACCESS_KEY_REDACTED]" in out
    assert summary.get("aws_access_key") == 1


# ── Phase 17b new patterns: JWT (existing but verify in this suite) ──


def test_jwt_three_segment_redacted():
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    out, summary = redact.redact(f"Token: {jwt}")
    assert "[JWT_REDACTED]" in out
    assert jwt not in out
    assert summary.get("jwt") == 1


def test_jwt_two_segment_not_matched():
    # A two-segment base64-ish string is not a JWT; do not redact as JWT.
    two_seg = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    out, summary = redact.redact(f"Header: {two_seg}")
    assert "[JWT_REDACTED]" not in out
    assert summary.get("jwt", 0) == 0


# ── Bearer token (generic) ────────────────────────────────────────────


def test_bearer_token_redacted():
    token = "abc123def456ghi789jkl012mno345pqr678stu901"  # 42 chars
    out, summary = redact.redact(f"Authorization: Bearer {token}")
    assert "Bearer <REDACTED_TOKEN>" in out
    assert token not in out
    assert summary.get("bearer_token") == 1


def test_bearer_short_value_not_matched():
    # Less than 40 chars should not match the generic Bearer pattern.
    out, summary = redact.redact("Bearer short123")
    assert "<REDACTED_TOKEN>" not in out
    assert summary.get("bearer_token", 0) == 0


def test_bearer_with_jwt_payload_labels_as_jwt():
    # JWT runs before Bearer; a `Bearer eyJ...` value should label as
    # JWT and the literal token body must be gone.
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    out, summary = redact.redact(f"Authorization: Bearer {jwt}")
    assert "[JWT_REDACTED]" in out
    assert jwt not in out
    assert summary.get("jwt") == 1


# ── Private keys (multi-line PEM) ─────────────────────────────────────


def test_rsa_private_key_redacted():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA1234567890abcdef\n"
        "ghijklmnopqrstuvwxyz1234567890==\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out, summary = redact.redact(f"Key follows:\n{pem}\nDone.")
    assert "<REDACTED_PRIVATE_KEY>" in out
    assert "MIIEpAIBAAKCAQEA" not in out
    assert summary.get("rsa_private_key") == 1


def test_generic_private_key_redacted():
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQD\n"
        "-----END PRIVATE KEY-----"
    )
    out, summary = redact.redact(pem)
    assert "<REDACTED_PRIVATE_KEY>" in out
    assert "MIIEvQIBADAN" not in out
    assert summary.get("private_key") == 1


def test_openssh_private_key_redacted():
    pem = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAA\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    out, summary = redact.redact(pem)
    assert "<REDACTED_PRIVATE_KEY>" in out
    assert summary.get("openssh_private_key") == 1


def test_pem_lookalike_not_redacted():
    # A PEM-shaped CERTIFICATE block is public, not a private key.
    cert = (
        "-----BEGIN CERTIFICATE-----\n"
        "MIIDdzCCAl+gAwIBAgIEAgAAuTANBgkqhkiG9w0BAQU\n"
        "-----END CERTIFICATE-----"
    )
    out, summary = redact.redact(cert)
    assert "<REDACTED_PRIVATE_KEY>" not in out
    assert summary.get("rsa_private_key", 0) == 0
    assert summary.get("private_key", 0) == 0


# ── AWS secret access key (context-aware) ─────────────────────────────


def test_aws_secret_access_key_lowercase_assignment_redacted():
    secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    out, summary = redact.redact(f'aws_secret_access_key = "{secret}"')
    assert "<REDACTED_AWS_SECRET>" in out
    assert secret not in out
    assert summary.get("aws_secret_access_key") == 1


def test_aws_secret_access_key_uppercase_envvar_redacted():
    secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    out, summary = redact.redact(f"export AWS_SECRET_ACCESS_KEY={secret}")
    assert "<REDACTED_AWS_SECRET>" in out
    assert secret not in out
    assert summary.get("aws_secret_access_key") == 1


def test_bare_40char_base64_without_context_not_redacted():
    # A 40-char base64-ish string without `aws_secret_access_key=` context
    # must not trigger the AWS-secret redaction (would false-positive on
    # too many things).
    blob = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    out, summary = redact.redact(f"random_blob = {blob}")
    assert "<REDACTED_AWS_SECRET>" not in out
    assert summary.get("aws_secret_access_key", 0) == 0


# ── GCP service account JSON ──────────────────────────────────────────


def test_gcp_service_account_json_redacted():
    sa = (
        '{"type": "service_account", "project_id": "my-proj-123", '
        '"private_key_id": "abc", "private_key": "-----BEGIN PRIVATE KEY-----\\n...", '
        '"client_email": "sa@my-proj.iam.gserviceaccount.com"}'
    )
    out, summary = redact.redact(f"sa_json = {sa}")
    assert "<REDACTED_GCP_SERVICE_ACCOUNT>" in out
    assert "my-proj-123" not in out
    assert "sa@my-proj.iam.gserviceaccount.com" not in out
    assert summary.get("gcp_service_account_json") == 1


def test_non_service_account_json_not_redacted():
    blob = '{"type": "user_account", "project_id": "my-proj-123"}'
    out, summary = redact.redact(blob)
    assert "<REDACTED_GCP_SERVICE_ACCOUNT>" not in out
    assert summary.get("gcp_service_account_json", 0) == 0


# ── GitHub PAT (fine-grained, new) ────────────────────────────────────


def test_github_fine_grained_pat_redacted():
    pat = "github_pat_" + "A" * 82
    out, summary = redact.redact(f"token={pat}")
    assert "[GITHUB_PAT_REDACTED]" in out
    assert pat not in out
    assert summary.get("github_pat_fine") == 1


def test_github_pat_lookalike_too_short_not_redacted():
    # `github_pat_` prefix with too short a suffix is not a real PAT.
    out, summary = redact.redact("env github_pat_short")
    assert "[GITHUB_PAT_REDACTED]" not in out
    assert summary.get("github_pat_fine", 0) == 0


# ── Slack tokens ──────────────────────────────────────────────────────


def test_slack_bot_token_redacted():
    # Synthetic low-entropy fixture; matches the redact regex
    # (`\bxox[bparso]-[0-9A-Za-z-]{20,}\b`) but is obviously not a real
    # token so GitHub push-protection / gitleaks doesn't flag it.
    token = "xoxb-" + "1" * 25
    out, summary = redact.redact(f"SLACK_BOT_TOKEN={token}")
    assert "<REDACTED_SLACK_TOKEN>" in out
    assert token not in out
    assert summary.get("slack_token") == 1


def test_slack_user_token_redacted():
    token = "xoxp-" + "2" * 25
    out, summary = redact.redact(token)
    assert "<REDACTED_SLACK_TOKEN>" in out
    assert summary.get("slack_token") == 1


def test_slack_lookalike_no_xox_prefix_not_redacted():
    # Looks Slack-token-shaped but missing the `xox` prefix.
    out, summary = redact.redact("foo-" + "3" * 25)
    assert "<REDACTED_SLACK_TOKEN>" not in out
    assert summary.get("slack_token", 0) == 0


# ── Snowflake account identifier ──────────────────────────────────────


def test_snowflake_account_redacted():
    out, summary = redact.redact("account = xy12345.us-east-1")
    assert "<REDACTED_SF_ACCOUNT>" in out
    assert "xy12345" not in out
    assert summary.get("snowflake_account") == 1


def test_snowflake_account_with_cloud_suffix_redacted():
    out, summary = redact.redact("SNOWFLAKE_ACCOUNT=ab98765.eu-west-1.aws")
    assert "<REDACTED_SF_ACCOUNT>" in out
    assert summary.get("snowflake_account") == 1


def test_snowflake_lookalike_too_many_digits_not_redacted():
    # 6 digits is not the canonical SF account shape (2 letters + 5
    # digits is the spec).
    out, summary = redact.redact("foo abc123456.us-east-1")
    assert "<REDACTED_SF_ACCOUNT>" not in out
    assert summary.get("snowflake_account", 0) == 0


# ── IPv4 ──────────────────────────────────────────────────────────────


def test_ipv4_private_redacted():
    out, summary = redact.redact("Internal DB at 10.0.0.5 on port 5432.")
    assert "<REDACTED_IPV4>" in out
    assert "10.0.0.5" not in out
    assert summary.get("ipv4") == 1


def test_ipv4_public_redacted():
    out, summary = redact.redact("Connect to 203.0.113.42 for the gateway.")
    assert "<REDACTED_IPV4>" in out
    assert summary.get("ipv4") == 1


def test_ipv4_loopback_allowlisted():
    out, summary = redact.redact("Bind localhost at 127.0.0.1 for dev.")
    assert "127.0.0.1" in out
    assert "<REDACTED_IPV4>" not in out
    assert summary.get("ipv4", 0) == 0


def test_ipv4_well_known_publics_allowlisted():
    out, summary = redact.redact(
        "Use 8.8.8.8 or 1.1.1.1 as DNS; bind 0.0.0.0 to all interfaces."
    )
    assert "8.8.8.8" in out
    assert "1.1.1.1" in out
    assert "0.0.0.0" in out
    assert "<REDACTED_IPV4>" not in out


def test_ipv4_version_string_not_redacted_as_ip():
    # Version strings like `terraform 1.5.7` have only 3 segments; the
    # IPv4 regex requires exactly 4 octets, so this must not match.
    out, summary = redact.redact("terraform version 1.5.7 installed")
    assert "<REDACTED_IPV4>" not in out
    assert summary.get("ipv4", 0) == 0


# ── IPv6 ──────────────────────────────────────────────────────────────


def test_ipv6_full_redacted():
    out, summary = redact.redact(
        "v6 addr: 2001:0db8:85a3:0000:0000:8a2e:0370:7334 here"
    )
    assert "<REDACTED_IPV6>" in out
    assert "2001:0db8" not in out
    assert summary.get("ipv6") == 1


def test_ipv6_abbreviated_redacted():
    out, summary = redact.redact("Loopback v6: ::1 is local")
    assert "<REDACTED_IPV6>" in out
    assert summary.get("ipv6") == 1


def test_ipv6_lookalike_cpp_scope_not_redacted():
    # `std::cout` is not an IPv6 address.
    out, summary = redact.redact("std::cout << foo;")
    assert "<REDACTED_IPV6>" not in out
    assert summary.get("ipv6", 0) == 0


# ── MAC address ───────────────────────────────────────────────────────


def test_mac_address_colon_separator_redacted():
    out, summary = redact.redact("Device MAC: 00:1B:44:11:3A:B7 reported")
    assert "<REDACTED_MAC>" in out
    assert "00:1B:44" not in out
    assert summary.get("mac_address") == 1


def test_mac_address_hyphen_separator_redacted():
    out, summary = redact.redact("MAC=AA-BB-CC-DD-EE-FF")
    assert "<REDACTED_MAC>" in out
    assert summary.get("mac_address") == 1


def test_mac_address_lookalike_5_groups_not_redacted():
    # Only 5 hex pairs is not a valid MAC.
    out, summary = redact.redact("partial 00:1B:44:11:3A here")
    assert "<REDACTED_MAC>" not in out
    assert summary.get("mac_address", 0) == 0


# ── format_summary ────────────────────────────────────────────────────


def test_format_summary_empty():
    assert redact.format_summary({}) == ""


def test_format_summary_singular_no_s():
    out = redact.format_summary({"email": 1})
    assert out == "1 email"


def test_format_summary_plural_address_es():
    # `IPv4 address` and `IPv6 address` and `MAC address` end in `s`,
    # so plural form must use `es`.
    out = redact.format_summary({"ipv4": 2})
    assert out == "2 IPv4 addresses"


def test_format_summary_private_key_labels_merged():
    # Multiple private-key flavors collapse to one human-readable line.
    out = redact.format_summary({"rsa_private_key": 1, "openssh_private_key": 1})
    assert out == "2 private keys"


def test_format_summary_multiple_categories():
    out = redact.format_summary({"email": 2, "jwt": 1, "ipv4": 3})
    parts = out.split(", ")
    assert "2 emails" in parts
    assert "1 JWT" in parts
    assert "3 IPv4 addresses" in parts


# ── Module-level invariants ───────────────────────────────────────────


def test_empty_input_safe():
    out, summary = redact.redact("")
    assert out == ""
    assert summary == {}


def test_none_input_safe():
    out, summary = redact.redact(None)
    assert out == ""
    assert summary == {}


def test_no_pii_input_unchanged():
    text = "Create an Okta SAML app for Workday with attribute statements."
    out, summary = redact.redact(text)
    assert out == text
    assert summary == {}


def test_redact_is_deterministic():
    text = "Email alice@x.com and bob@y.com today."
    out1, _ = redact.redact(text)
    out2, _ = redact.redact(text)
    assert out1 == out2


# ── Performance ───────────────────────────────────────────────────────


def test_performance_10kb_prompt_under_100ms():
    # Build a 10KB prompt with a sprinkling of patterns so the regex
    # set has real work to do, not just empty scans.
    chunk = (
        "Generate Terraform for Okta SAML app for alice@example.com, "
        "with bearer token abc123def456ghi789jkl012mno345pqr678stu9, "
        "internal DB at 10.0.0.5, MAC 00:1B:44:11:3A:B7. "
    )
    text = chunk * (10240 // len(chunk) + 1)
    text = text[:10240]
    start = time.perf_counter()
    out, summary = redact.redact(text)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 100, f"redact took {elapsed_ms:.1f}ms on a 10KB prompt"
    # And it actually redacted something.
    assert summary.get("email", 0) > 0


# ── Standalone runner ─────────────────────────────────────────────────


if __name__ == "__main__":
    import traceback
    failures = []
    tests = [
        # Existing pattern smoke tests
        test_email_redacted,
        test_phone_redacted,
        test_ssn_redacted,
        test_credit_card_luhn_validated,
        test_credit_card_invalid_luhn_not_redacted,
        test_anthropic_key_redacted,
        test_github_pat_classic_redacted,
        test_aws_access_key_id_redacted,
        # JWT
        test_jwt_three_segment_redacted,
        test_jwt_two_segment_not_matched,
        # Bearer
        test_bearer_token_redacted,
        test_bearer_short_value_not_matched,
        test_bearer_with_jwt_payload_labels_as_jwt,
        # Private keys
        test_rsa_private_key_redacted,
        test_generic_private_key_redacted,
        test_openssh_private_key_redacted,
        test_pem_lookalike_not_redacted,
        # AWS secret
        test_aws_secret_access_key_lowercase_assignment_redacted,
        test_aws_secret_access_key_uppercase_envvar_redacted,
        test_bare_40char_base64_without_context_not_redacted,
        # GCP SA JSON
        test_gcp_service_account_json_redacted,
        test_non_service_account_json_not_redacted,
        # GitHub fine PAT
        test_github_fine_grained_pat_redacted,
        test_github_pat_lookalike_too_short_not_redacted,
        # Slack
        test_slack_bot_token_redacted,
        test_slack_user_token_redacted,
        test_slack_lookalike_no_xox_prefix_not_redacted,
        # Snowflake
        test_snowflake_account_redacted,
        test_snowflake_account_with_cloud_suffix_redacted,
        test_snowflake_lookalike_too_many_digits_not_redacted,
        # IPv4
        test_ipv4_private_redacted,
        test_ipv4_public_redacted,
        test_ipv4_loopback_allowlisted,
        test_ipv4_well_known_publics_allowlisted,
        test_ipv4_version_string_not_redacted_as_ip,
        # IPv6
        test_ipv6_full_redacted,
        test_ipv6_abbreviated_redacted,
        test_ipv6_lookalike_cpp_scope_not_redacted,
        # MAC
        test_mac_address_colon_separator_redacted,
        test_mac_address_hyphen_separator_redacted,
        test_mac_address_lookalike_5_groups_not_redacted,
        # format_summary
        test_format_summary_empty,
        test_format_summary_singular_no_s,
        test_format_summary_plural_address_es,
        test_format_summary_private_key_labels_merged,
        test_format_summary_multiple_categories,
        # Invariants
        test_empty_input_safe,
        test_none_input_safe,
        test_no_pii_input_unchanged,
        test_redact_is_deterministic,
        # Performance
        test_performance_10kb_prompt_under_100ms,
    ]
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception:
            failures.append(t.__name__)
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    if failures:
        print(f"\n{len(failures)} failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} passed")
