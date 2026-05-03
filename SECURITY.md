# Security overview

This document describes the security posture of the TF Tool: what gets logged, what's encrypted, what isn't, and what's explicitly out of scope today. It is intended for IT teams reviewing whether to deploy this tool against their Okta org, AWS account, or GCP project.

## Architecture

Single-tenant Streamlit Cloud deployment per customer. Browser → Streamlit Cloud (US-region, encrypted at rest by Snowflake's underlying storage) → Anthropic API → GitHub. No customer data is persisted outside of (a) the GitHub repo configured in `GITHUB_REPO` and (b) Streamlit Cloud's session memory.

## Authentication

Google OAuth via Streamlit's built-in `[auth]` configuration. No SAML, no SCIM in this build (see "Out of scope" below). The auth gate sits in front of every route; unauthenticated users see a sign-in screen and nothing else.

## Authorization (RBAC)

Roles are configured in `[roles]` of `.streamlit/secrets.toml`:

| Role | Capabilities |
|---|---|
| `admin` | All features, manage `roles.toml` via the in-app sidebar, view all users' audit/cost. |
| `editor` | All generation/push, sees only own audit/cost. |
| `contributor` | Generation OK; push only to repos owned by their GitHub username (e.g. `alice@x.com` can push to `alice/foo` but not `acme/foo`). |
| `viewer` | Parse and view outputs only. No generation, no push. |

Default for any signed-in user not in the role map is `viewer` (most restrictive). See `roles.py:_CAPS` for the full capability matrix and `roles.py:can_push_to` for the push-scope rule.

Example `[roles]` block:

```toml
[roles]
admin       = ["alice@example.com", "ops@example.com"]
editor      = ["bob@example.com"]
contributor = ["intern@example.com"]
default     = "viewer"

[quotas]
admin       = 0      # 0 means unlimited
editor      = 5.00
contributor = 2.00
viewer      = 0.50
```

## Data path and what's logged

| Action | Sent to Anthropic | Logged where |
|---|---|---|
| Sign in / sign out | Nothing | Audit log on GitHub |
| Parse intent | The user's prompt (after PII redaction; see below) | Audit log + cost log |
| Generate / regenerate / fix | Same as parse, plus the parsed intent JSON | Audit log + cost log |
| Push to GitHub | Generated HCL/Python files only — no prompt content | Audit log + commit on GitHub |
| Env refresh | Nothing (live-context calls go to Okta / AWS / GCP, not Anthropic) | Audit log |

Audit records live in `_tftool/audit/<email-hash>.jsonl` in the configured GitHub repo. Append-only; the application never edits or deletes records. Each record carries a UUID `request_id`, the actor's email, the action name, the inferred resource type, the output mode, the cost-estimate in USD, and the first 200 characters of the redacted prompt for context.

Cost records live in `_tftool/usage/<email-hash>.json`, keyed by UTC date. Used by the per-user daily quota gate.

## PII redaction

`redact.py` strips the following from every prompt **before** it leaves Streamlit for Anthropic:

- Email addresses
- US-style phone numbers
- US Social Security Numbers (formatted as NNN-NN-NNNN)
- Credit card numbers (only when the digits pass a Luhn check)
- API keys: Anthropic (`sk-ant-...`), OpenAI (`sk-...`), Stripe (`(sk|pk|rk)_(live|test)_...`), GitHub PATs (`ghp_...`, `github_pat_...`)
- AWS access key IDs (`AKIA...`, `ASIA...`, etc.)
- JWT tokens (3-part base64url)

Patterns intentionally NOT redacted because they are infrastructure context the model needs:

- IP addresses
- Hostnames and full URLs
- GCP project IDs
- Okta organization names
- SAML entity IDs and ARNs

Admins can toggle redaction off per session via the sidebar. Every redaction event is audit-logged with the per-category counts (no values).

## Per-user daily cost cap

Every Anthropic API call's usage object is intercepted in `cost.py:wrap_client` and accumulated against the signed-in user's UTC-day total. When today's spend reaches the role-configured cap (default $5/day for editors, $0.50/day for viewers, unlimited for admins), the parse and generate actions are blocked with a friendly message until the next UTC midnight.

## Secrets in transit and at rest

Secrets live in Streamlit Cloud's secret manager (encrypted by their underlying storage; not customer-managed keys). They are never written to the repo, never returned in audit records, and never echoed to the UI. The first 8 characters of the Anthropic key are surfaced only when validation fails (so you can see whether the wrong key shape was pasted). API keys can be rotated at any time by editing the Streamlit Cloud secrets and rebooting the app.

## Session security

- 30-minute idle timeout. After 30 minutes of no Streamlit activity, the session is wiped and the user is forced to sign in again. Implemented at the top of `app.py` with `last_activity_ts` in session_state.
- Sign-out clears the session and is audit-logged.

## Secret rotation reminders

Admins see a sidebar warning when any tracked secret is older than its target rotation cadence:

| Secret | Target cadence |
|---|---|
| `ANTHROPIC_API_KEY` | 90 days |
| `GITHUB_TOKEN` | 90 days |
| `OKTA_API_TOKEN` | 180 days |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | 90 days |
| `GCP_SA_JSON` | 180 days |

Rotation dates are recorded in `_tftool/secret_rotation.json` (admin-edited via the in-app widget — coming in B.3 — or directly in GitHub).

## What's NOT in this build (Thread C — future)

- SAML SSO (Streamlit Cloud only supports Google OAuth via `st.login`).
- SCIM provisioning of users into the tool.
- Customer-managed encryption keys (Streamlit Cloud uses Snowflake's own encryption-at-rest keys).
- EU / non-US region deployment.
- Multi-tenant org isolation within one app instance (current model is one Streamlit Cloud app per customer).
- SLA framework, Data Processing Agreement template, SOC2 Type 2 attestation.

If a customer requires any of the above on day 1, deploy a single-tenant per-customer Streamlit Cloud instance behind their own SAML proxy and route their audit log to a GitHub repo they own. That's the supported short-term path while Thread C (self-hosted Docker / GKE rebuild with native SAML/SCIM/CMK) is being built.

## Reporting a vulnerability

Email `cyrillical@gmail.com` with subject `[TF Tool security]`. Include reproduction steps. We aim to acknowledge within 1 business day and patch within 7 days for high-severity issues.
