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
| Push to GitHub | Generated HCL/Python files only (no prompt content) | Audit log + commit on GitHub |
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

| Secret | Target cadence | Surface |
|---|---|---|
| `ANTHROPIC_API_KEY` | 90 days | Streamlit, CLI, HTTP, Slack, JIRA |
| `GITHUB_TOKEN` | 90 days | Streamlit, CLI, HTTP, Slack, JIRA |
| `OKTA_API_TOKEN` | 180 days | Streamlit, CLI |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | 90 days | Streamlit, CLI |
| `GCP_SA_JSON` | 180 days | Streamlit, CLI |
| `TFGEN_API_KEY` | 90 days | HTTP API shared secret |
| `SLACK_SIGNING_SECRET` | 180 days | Slack slash command verification |
| `JIRA_WEBHOOK_SECRET` | 180 days | JIRA webhook HMAC verification |
| `JIRA_API_TOKEN` | 90 days | JIRA REST callback (paired with `JIRA_USER_EMAIL`) |
| `JAMF_CLIENT_ID` / `JAMF_CLIENT_SECRET` | 90 days | JAMF Pro live env-context oauth2 |

Rotation dates are recorded in `_tftool/secret_rotation.json` (admin-edited via the in-app sidebar widget, or directly in GitHub).

### Rolling the headless-surface secrets

- **`TFGEN_API_KEY`**: generate a new random string (`openssl rand -hex 32`), update the Vercel env var, redeploy. Notify every script / Slack/JIRA bot that holds the old value. Old key stops working the moment the new value is bound; rotate during a low-traffic window.
- **`SLACK_SIGNING_SECRET`**: rotate from the Slack app's Basic Information page in the Slack admin console. Slack supports a 24-hour overlap where both secrets verify; copy the new secret to Vercel before the overlap ends.
- **`JIRA_WEBHOOK_SECRET`**: shared secret you control end-to-end. Update the JIRA Automation rule (or front proxy) and the Vercel env var in the same window. Old signatures fail validation immediately, so set both sides simultaneously.
- **`JIRA_API_TOKEN`**: regenerate at id.atlassian.com → Security → API tokens. Old tokens are revoked instantly; expect a brief callback failure if the JIRA bot account's old token was in use mid-flight.
- **`JAMF_CLIENT_ID` / `JAMF_CLIENT_SECRET`**: rotate from JAMF Pro Settings → System → API Roles and Clients. Generate a new client secret on the existing client (preserves the role binding); update Streamlit Cloud secrets, then revoke the old secret. Roles needed for live env-context: read on Policies, Computer Groups, Scripts, Packages, and Computer Extension Attributes.

## Headless surfaces (CLI, HTTP, Slack, JIRA)

Phase 10 Track 2 added four entry points that share the same `core.service.generate()` pipeline and the same `audit.py` + `cost.py` storage as the Streamlit app. Differences worth flagging for an IT review:

- **No Google OAuth gate**: the Streamlit `[auth]` block does not apply to CLI / HTTP / Slack / JIRA. Each surface has its own auth (env var, shared header secret, signing secret, HMAC) summarized in the README. Treat each surface as a separate authorization plane that needs its own access review.
- **PII redaction is preserved**: every surface calls `redact.redact()` before the prompt reaches Anthropic. Same patterns as the Streamlit app; same audit-log entries with per-category counts.
- **Quota model is a single shared cap, not per-actor**: HTTP / Slack / JIRA all share `TFGEN_HTTP_DAILY_QUOTA_USD` (default $5/day, summed across all keys / Slack users / JIRA actors). Per-key or per-Slack-user quotas require a `[api_keys]` extension to `roles.toml` and are explicitly deferred until traffic justifies it.
- **Audit identifier shape**: HTTP uses `sha256(api_key)[:16]`, Slack uses `sha256(slack_user_id)[:16]`, JIRA uses `sha256(creator_email)[:16]`. Different hash inputs, same on-disk JSONL files, so per-actor history is queryable by hash. The hash itself never round-trips back to the underlying identifier from the log alone.
- **GitHub push uses the server's `GITHUB_TOKEN`, not the caller's**: callers pushing through `/api/push` or via the Slack/JIRA flow share the bot's identity. If a customer needs caller-supplied tokens, that's a `PushRequest.github_token` field gated behind a separate role; not in this build.
- **No callback secrets in transit to Anthropic**: the JIRA REST credentials and Slack `response_url` strings are never included in the prompt sent to Claude. They live in env vars / the inbound payload only.
- **Slack and JIRA push branches are predictable**: Slack pushes to `tfgen-slack`, JIRA pushes to `jira/<issue_key>`. A reviewer can carve a branch-protection rule that requires PR review on either prefix before merge.

## JAMF Pro apply runbook

Generated `terraform_jamf_hcl` outputs ship with a top-of-file `# JAMF APPLY RUNBOOK` comment listing operational constraints that Terraform itself does not enforce:

1. **`terraform apply -parallelism=1`** is required. JAMF Pro produces inconsistent behaviour at the default parallelism of 10 (resources race against each other on the same JSS API endpoints; results are non-deterministic).
2. **`jamfpro_load_balancer_lock = true`** must be set in the provider block when targeting JAMF Cloud (`*.jamfcloud.com`). The 60-second LB cookie propagation delay otherwise causes drift between parallel API calls landing on different web app members.
3. **Eventual consistency**: an immediate `terraform plan` after `apply` may show drift. Re-run plan a few minutes later before assuming a real diff exists.

Reviewers should reject any PR that contains JAMF HCL without the runbook header. The validator pass flags missing runbook headers as an issue, but the reviewer-side gate is the more reliable check.

## Additions since Thread A

A few non-runtime additions have shipped on top of the Thread A baseline; calling them out so reviewers know what's in the binary:

- **In-app feedback widget** (`feedback.py`): submit button in the help drawer posts the text + the signed-in user's email to a new GitHub issue in `GITHUB_REPO`. Treat this as a data-export channel; whatever a user types in the feedback box leaves Streamlit and lands in the configured GitHub repo. The text is not run through `redact.py`. Disable by removing the widget from `ui/account.py` if your org needs to gate outbound text.
- **Terraform-validate guardrail** (`tf_validate.py` + `qa_runner.py --terraform-validate`, default ON): catches schema-drift output bugs that would otherwise emit invalid HCL to the customer's GitHub repo. Not a runtime security control, but it directly reduces the chance of a generation regression shipping to production Terraform.
- **Stop hook** (`.claude/check-unpushed.ps1` + `.claude/settings.json`): developer-tooling only. Blocks ending a Claude Code turn with unpushed commits in this repo. No runtime impact on the deployed app.

## What's NOT in this build (Thread C, future)

- SAML SSO (Streamlit Cloud only supports Google OAuth via `st.login`).
- SCIM provisioning of users into the tool.
- Customer-managed encryption keys (Streamlit Cloud uses Snowflake's own encryption-at-rest keys).
- EU / non-US region deployment.
- Multi-tenant org isolation within one app instance (current model is one Streamlit Cloud app per customer).
- SLA framework, Data Processing Agreement template, SOC2 Type 2 attestation.

If a customer requires any of the above on day 1, deploy a single-tenant per-customer Streamlit Cloud instance behind their own SAML proxy and route their audit log to a GitHub repo they own. That's the supported short-term path while Thread C (self-hosted Docker / GKE rebuild with native SAML, SCIM, CMK) is being built.

## Reporting a vulnerability

Email `cyrillical@gmail.com` with subject `[TF Tool security]`. Include reproduction steps. We aim to acknowledge within 1 business day and patch within 7 days for high-severity issues.
