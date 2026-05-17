"""Tests for `generator.output_scanner` (Phase 18b).

Standalone-runnable: `python tests/test_output_scanner.py`.
pytest-compatible: `pytest tests/test_output_scanner.py -q`.

Coverage:
- One positive test per redact pattern category (private key, GCP SA
  JSON, AWS access key, AWS secret, Slack token, Snowflake account,
  IPv4, IPv6, MAC, JWT, Bearer, Anthropic / OpenAI / Stripe / GitHub
  PATs, email, SSN, phone).
- Negative test: clean HCL with no secrets returns an empty list.
- Multi-finding test: HCL with three different secret shapes returns
  three findings with the correct line numbers.
- Snippet redaction invariant: the `snippet` field never contains the
  raw matched secret; the matched bytes are always replaced by `<...>`.
- IPv4 well-known allowlist: 127.0.0.1 / 8.8.8.8 do not flag.
- Internal bookkeeping keys (leading underscore) are never scanned.

All credential fixtures use synthetic low-entropy bodies so gitleaks
does not block the commit at push time. The point of this test file
is to exercise pattern shape, not to embed real keys.
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from generator.output_scanner import (
    scan_outputs_for_secrets,
    format_findings,
    _OUTPUT_KEYS,
)


# ── Helpers ────────────────────────────────────────────────────────────


def _categories(findings: list[dict]) -> set[str]:
    return {f["category"] for f in findings}


def _snippet_is_redacted(findings: list[dict], raw_secret: str) -> bool:
    """Every finding's snippet must contain `<...>` and must not contain
    the raw secret bytes. Returns True only when both hold for every
    finding."""
    for f in findings:
        if raw_secret in f["snippet"]:
            return False
        if "<...>" not in f["snippet"]:
            return False
    return True


# ── Positive tests per pattern category ────────────────────────────────


def test_private_key_pem_block_detected():
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        + "a" * 200
        + "\n-----END PRIVATE KEY-----"
    )
    outputs = {"terraform_jamf_hcl": f"resource \"jamf_script\" \"x\" {{\n  body = <<EOT\n{pem}\nEOT\n}}\n"}
    findings = scan_outputs_for_secrets(outputs)
    assert "private_key" in _categories(findings)
    assert all(f["key"] == "terraform_jamf_hcl" for f in findings if f["category"] == "private_key")


def test_rsa_private_key_pem_block_detected():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        + "b" * 200
        + "\n-----END RSA PRIVATE KEY-----"
    )
    outputs = {"terraform_okta_hcl": pem}
    findings = scan_outputs_for_secrets(outputs)
    assert "rsa_private_key" in _categories(findings)


def test_gcp_service_account_json_detected():
    sa_json = (
        '{"type": "service_account", "project_id": "p", '
        '"private_key_id": "k", "client_email": "x@p.iam.gserviceaccount.com", '
        '"client_x509_cert_url": "https://example"}'
    )
    outputs = {"terraform_gcp_hcl": f"variable \"sa\" {{ default = {sa_json} }}"}
    findings = scan_outputs_for_secrets(outputs)
    assert "gcp_service_account_json" in _categories(findings)


def test_aws_access_key_detected():
    # AKIAIOSFODNN7EXAMPLE is the AWS documented example access key ID,
    # low-entropy and gitleaks-safe.
    outputs = {"terraform_lambda_hcl": 'access_key_id = "AKIAIOSFODNN7EXAMPLE"'}
    findings = scan_outputs_for_secrets(outputs)
    assert "aws_access_key" in _categories(findings)


def test_aws_secret_assignment_detected():
    secret_body = "A" * 40
    outputs = {
        "terraform_lambda_hcl": f'aws_secret_access_key = "{secret_body}"',
    }
    findings = scan_outputs_for_secrets(outputs)
    assert "aws_secret_access_key" in _categories(findings)


def test_slack_bot_token_detected():
    token = "xoxb-" + "1" * 25
    outputs = {"lambda_python": f'SLACK_TOKEN = "{token}"'}
    findings = scan_outputs_for_secrets(outputs)
    assert "slack_token" in _categories(findings)


def test_snowflake_account_detected():
    outputs = {"terraform_snowflake_hcl": 'account = "xy12345.us-east-1"'}
    findings = scan_outputs_for_secrets(outputs)
    assert "snowflake_account" in _categories(findings)


def test_ipv4_private_detected():
    outputs = {"terraform_okta_hcl": "ip = \"10.0.0.5\""}
    findings = scan_outputs_for_secrets(outputs)
    assert "ipv4" in _categories(findings)


def test_ipv6_full_detected():
    outputs = {"terraform_okta_hcl": "ip = \"2001:0db8:85a3:0000:0000:8a2e:0370:7334\""}
    findings = scan_outputs_for_secrets(outputs)
    assert "ipv6" in _categories(findings)


def test_mac_address_detected():
    outputs = {"terraform_jamf_hcl": "mac_address = \"00:1B:44:11:3A:B7\""}
    findings = scan_outputs_for_secrets(outputs)
    assert "mac_address" in _categories(findings)


def test_jwt_detected():
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    outputs = {"lambda_python": f'TOKEN = "{jwt}"'}
    findings = scan_outputs_for_secrets(outputs)
    assert "jwt" in _categories(findings)


def test_bearer_token_detected():
    token_body = "a" * 40
    outputs = {"lambda_python": f'headers = {{"Authorization": "Bearer {token_body}"}}'}
    findings = scan_outputs_for_secrets(outputs)
    assert "bearer_token" in _categories(findings)


def test_anthropic_api_key_detected():
    key = "sk-ant-api03-" + "A" * 93
    outputs = {"lambda_python": f'ANTHROPIC_API_KEY = "{key}"'}
    findings = scan_outputs_for_secrets(outputs)
    assert "api_key_anthropic" in _categories(findings)


def test_openai_api_key_detected():
    key = "sk-" + "B" * 40
    outputs = {"lambda_python": f'OPENAI_API_KEY = "{key}"'}
    findings = scan_outputs_for_secrets(outputs)
    assert "api_key_openai" in _categories(findings)


def test_stripe_api_key_detected():
    key = "sk_live_" + "C" * 30
    outputs = {"lambda_python": f'STRIPE = "{key}"'}
    findings = scan_outputs_for_secrets(outputs)
    assert "api_key_stripe" in _categories(findings)


def test_github_pat_classic_detected():
    pat = "ghp_" + "a" * 36
    outputs = {"lambda_python": f'TOKEN = "{pat}"'}
    findings = scan_outputs_for_secrets(outputs)
    assert "github_pat" in _categories(findings)


def test_github_pat_fine_grained_detected():
    pat = "github_pat_" + "b" * 60
    outputs = {"lambda_python": f'TOKEN = "{pat}"'}
    findings = scan_outputs_for_secrets(outputs)
    assert "github_pat_fine" in _categories(findings)


def test_email_detected():
    outputs = {"terraform_okta_hcl": '# Contact: alice@example.com\n'}
    findings = scan_outputs_for_secrets(outputs)
    assert "email" in _categories(findings)


def test_ssn_detected():
    outputs = {"lambda_python": '# user SSN 123-45-6789'}
    findings = scan_outputs_for_secrets(outputs)
    assert "ssn" in _categories(findings)


def test_phone_detected():
    outputs = {"terraform_okta_hcl": '# helpdesk 555-123-4567'}
    findings = scan_outputs_for_secrets(outputs)
    assert "phone_us" in _categories(findings)


# ── Negative tests ────────────────────────────────────────────────────


def test_clean_hcl_returns_empty():
    outputs = {
        "terraform_okta_hcl": (
            'resource "okta_app_saml" "x" {\n'
            '  label = "App"\n'
            '  sso_url = "https://example.com/sso"\n'
            '}\n'
        ),
        "terraform_lambda_hcl": "",
        "lambda_python": "def handler(event, context):\n    return event\n",
    }
    findings = scan_outputs_for_secrets(outputs)
    assert findings == []


def test_empty_dict_returns_empty():
    assert scan_outputs_for_secrets({}) == []


def test_none_value_safe():
    outputs = {"terraform_okta_hcl": None, "lambda_python": ""}
    findings = scan_outputs_for_secrets(outputs)
    assert findings == []


def test_non_dict_input_safe():
    assert scan_outputs_for_secrets(None) == []  # type: ignore[arg-type]
    assert scan_outputs_for_secrets("not a dict") == []  # type: ignore[arg-type]


def test_ipv4_well_known_allowlist_not_flagged():
    outputs = {"terraform_okta_hcl": 'ip = "127.0.0.1"\nip2 = "8.8.8.8"\nip3 = "1.1.1.1"\n'}
    findings = scan_outputs_for_secrets(outputs)
    assert "ipv4" not in _categories(findings)


def test_internal_underscore_key_not_scanned():
    # Even if a hostile dict carries a private key under a leading-
    # underscore key, the scanner ignores it (it's not user-visible).
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        + "a" * 200
        + "\n-----END PRIVATE KEY-----"
    )
    outputs = {"_secret_scan_findings": pem, "_internal": pem}
    findings = scan_outputs_for_secrets(outputs)
    assert findings == []


# ── Multi-finding test ─────────────────────────────────────────────────


def test_multi_finding_line_numbers_correct():
    # Three distinct secret shapes on three distinct lines. We expect at
    # least three findings (some shapes may match more than one pattern)
    # and the line numbers must match the source layout.
    src_lines = [
        'resource "okta_app_saml" "demo" {',          # line 1
        '  label = "Demo"',                            # line 2
        '  contact_email = "alice@example.com"',       # line 3, email
        '  access_key = "AKIAIOSFODNN7EXAMPLE"',       # line 4, aws_access_key
        '  helpdesk_phone = "555-123-4567"',           # line 5, phone_us
        '}',                                            # line 6
    ]
    outputs = {"terraform_okta_hcl": "\n".join(src_lines) + "\n"}
    findings = scan_outputs_for_secrets(outputs)
    cats_by_line = {f["line"]: f["category"] for f in findings}
    assert cats_by_line.get(3) == "email"
    assert cats_by_line.get(4) == "aws_access_key"
    assert cats_by_line.get(5) == "phone_us"
    assert len(findings) >= 3


def test_findings_sorted_by_line():
    src_lines = [
        '# alice@example.com',                          # line 1, email
        'access_key = "AKIAIOSFODNN7EXAMPLE"',          # line 2, aws_access_key
        'phone = "555-123-4567"',                       # line 3, phone_us
    ]
    outputs = {"terraform_okta_hcl": "\n".join(src_lines)}
    findings = scan_outputs_for_secrets(outputs)
    line_numbers = [f["line"] for f in findings]
    assert line_numbers == sorted(line_numbers)


# ── Snippet redaction invariant ────────────────────────────────────────


def test_snippet_does_not_contain_raw_secret():
    key = "AKIAIOSFODNN7EXAMPLE"
    outputs = {"terraform_lambda_hcl": f'access_key_id = "{key}"'}
    findings = scan_outputs_for_secrets(outputs)
    assert len(findings) >= 1
    assert _snippet_is_redacted(findings, key)


def test_snippet_redacts_private_key_block():
    body = "a" * 200
    pem = f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----"
    outputs = {"terraform_okta_hcl": pem}
    findings = scan_outputs_for_secrets(outputs)
    assert len(findings) >= 1
    # The body chunk and the BEGIN marker should both vanish from the
    # snippet for the multi-line PEM (collapsed to the first line with
    # the matched bytes replaced).
    for f in findings:
        assert body not in f["snippet"]
        assert "<...>" in f["snippet"]


def test_snippet_redacts_jwt():
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    outputs = {"lambda_python": f'TOKEN = "{jwt}"'}
    findings = scan_outputs_for_secrets(outputs)
    assert len(findings) >= 1
    assert _snippet_is_redacted(findings, jwt)


# ── format_findings ────────────────────────────────────────────────────


def test_format_findings_empty():
    assert format_findings([]) == ""


def test_format_findings_renders_one_line_per_finding():
    findings = [
        {"key": "terraform_okta_hcl", "category": "email", "line": 3, "snippet": "x = <...>"},
        {"key": "lambda_python", "category": "jwt", "line": 7, "snippet": "y = <...>"},
    ]
    out = format_findings(findings)
    assert "Secret-shape 'email' detected in terraform_okta_hcl line 3" in out
    assert "Secret-shape 'jwt' detected in lambda_python line 7" in out
    assert out.count("\n") == 1  # two lines, one newline between them


# ── Module-level invariants ────────────────────────────────────────────


def test_output_keys_contains_expected_set():
    expected = {
        "terraform_okta_hcl", "terraform_lambda_hcl", "terraform_gcp_hcl",
        "terraform_jamf_hcl", "fleet_gitops_yaml", "terraform_fleet_hcl",
        "terraform_snowflake_hcl", "lambda_python", "cloud_function_python",
        "optional_tf",
    }
    assert expected.issubset(set(_OUTPUT_KEYS))


# ── Standalone runner ──────────────────────────────────────────────────


if __name__ == "__main__":
    import traceback
    failures = []
    tests = [
        # Positive per-category
        test_private_key_pem_block_detected,
        test_rsa_private_key_pem_block_detected,
        test_gcp_service_account_json_detected,
        test_aws_access_key_detected,
        test_aws_secret_assignment_detected,
        test_slack_bot_token_detected,
        test_snowflake_account_detected,
        test_ipv4_private_detected,
        test_ipv6_full_detected,
        test_mac_address_detected,
        test_jwt_detected,
        test_bearer_token_detected,
        test_anthropic_api_key_detected,
        test_openai_api_key_detected,
        test_stripe_api_key_detected,
        test_github_pat_classic_detected,
        test_github_pat_fine_grained_detected,
        test_email_detected,
        test_ssn_detected,
        test_phone_detected,
        # Negative
        test_clean_hcl_returns_empty,
        test_empty_dict_returns_empty,
        test_none_value_safe,
        test_non_dict_input_safe,
        test_ipv4_well_known_allowlist_not_flagged,
        test_internal_underscore_key_not_scanned,
        # Multi-finding
        test_multi_finding_line_numbers_correct,
        test_findings_sorted_by_line,
        # Snippet redaction
        test_snippet_does_not_contain_raw_secret,
        test_snippet_redacts_private_key_block,
        test_snippet_redacts_jwt,
        # format_findings
        test_format_findings_empty,
        test_format_findings_renders_one_line_per_finding,
        # Invariants
        test_output_keys_contains_expected_set,
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
