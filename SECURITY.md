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

`redact.py` strips the following from every prompt **before** it leaves Streamlit for Anthropic. Categories below are listed in the order patterns are applied (longer / more specific first, so they consume bytes before broader patterns match).

**Multi-line credentials (PEM blocks):**

- RSA private keys (`-----BEGIN RSA PRIVATE KEY----- ... -----END RSA PRIVATE KEY-----`)
- OpenSSH private keys (`-----BEGIN OPENSSH PRIVATE KEY-----`)
- EC private keys (`-----BEGIN EC PRIVATE KEY-----`)
- DSA private keys (`-----BEGIN DSA PRIVATE KEY-----`)
- Generic PKCS#8 private keys (`-----BEGIN PRIVATE KEY-----`)

**Cloud credential blobs:**

- GCP service account JSON (`{"type": "service_account", ...}`)
- AWS access key IDs (`AKIA`, `ASIA`, `AROA`, `AIDA`, `ANPA`, `ANVA`, `AGPA` prefixes)
- AWS secret access keys (40-char base64-ish value, context-aware: only when preceded by `aws_secret_access_key=` or `AWS_SECRET_ACCESS_KEY=`)

**Vendor API keys:**

- Anthropic (`sk-ant-...`, covers `sk-ant-api03-...`)
- OpenAI (`sk-...`, 32+ chars)
- Stripe (`(sk|pk|rk)_(live|test)_...`)
- GitHub classic PAT (`ghp_...`)
- GitHub fine-grained PAT (`github_pat_...`, 50+ char suffix)
- Slack tokens (`xoxb-`, `xoxp-`, `xoxa-`, `xoxr-`, `xoxs-`, `xoxo-`)
- Snowflake account identifier (`[a-z]{2}\d{5}\.<region>[.<cloud>]`)

**Generic credentials:**

- JWT tokens (3-part base64url with `eyJ` prefix)
- Bearer tokens (`Bearer <40+ char body>`; JWT-shaped bodies still label as JWT thanks to ordering)

**Personally identifiable information:**

- Email addresses
- US-style phone numbers
- US Social Security Numbers (NNN-NN-NNNN)
- Credit card numbers (only when the digits pass a Luhn check)

**Network identifiers:**

- IPv4 addresses, except a small allowlist of well-known publics that show up constantly in instructional context: `0.0.0.0`, `127.0.0.1`, `1.1.1.1`, `1.0.0.1`, `8.8.8.8`, `8.8.4.4`
- IPv6 addresses (full and abbreviated forms, including `::1` and `::`)
- MAC addresses (6 hex pairs separated by `:` or `-`)

Patterns intentionally NOT redacted because they are infrastructure context the model needs to generate correct Terraform:

- Hostnames and full URLs
- GCP project IDs
- Okta organization names
- SAML entity IDs and AWS ARNs
- Role names
- Terraform-side resource identifiers

Admins can toggle redaction off per session via the sidebar. Every redaction event is audit-logged with the per-category counts (no values). Unit tests at `tests/test_redact.py` cover one positive plus one negative case per pattern, and a 10KB-prompt performance smoke that asserts the full pattern set runs in under 100ms.

## Per-user daily cost cap

Every Anthropic API call's usage object is intercepted in `cost.py:wrap_client` and accumulated against the signed-in user's UTC-day total. When today's spend reaches the role-configured cap (default $5/day for editors, $0.50/day for viewers, unlimited for admins), the parse and generate actions are blocked with a friendly message until the next UTC midnight.

### Per-actor headless quotas (Phase 21b)

Headless callers (HTTP, Slack, JIRA) inherit their daily cap from one of three sources, resolved in order:

1. `roles.toml [api_keys.<entry>].daily_quota_usd` matched to the caller's `actor_id` (per-key quota; recommended for production).
2. `TFGEN_HTTP_DAILY_QUOTA_USD` env var (legacy single shared cap; preserves the pre-Phase-21 contract for `TFGEN_API_KEY` users).
3. `[cost] daily_cap_usd` in `roles.toml` (final fallback; default $5/day).

A quota of `0` means unlimited. `cost.quota_used_by_actor(actor_id)` is the canonical accessor for current-day spend; it is an alias for `today_usd` named to match the Phase 21 spec language. Every quota check is mirrored to `audit.log` as a `quota_check` event so abuse patterns surface in the audit log without requiring a separate metric pipeline.

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
| `FLEET_URL` / `FLEET_API_TOKEN` | 90 days | Fleet MDM. Same env vars cover both GitOps (fleetctl apply) and Terraform (l-teles/fleetdm provider) output paths. |
| `SNOWFLAKE_ACCOUNT` / `SNOWFLAKE_USER` / `SNOWFLAKE_PRIVATE_KEY` / `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` / `SNOWFLAKE_ROLE` / `SNOWFLAKE_WAREHOUSE` | 90 days | Snowflake Terraform (snowflakedb/snowflake v2.x). Key-pair auth required (Snowflake deprecated password Nov 2025). |

Rotation dates are recorded in `_tftool/secret_rotation.json` (admin-edited via the in-app sidebar widget, or directly in GitHub).

### Local pre-commit secrets scan

Developers should install the repo's pre-commit hooks (`pip install pre-commit && pre-commit install`) so that gitleaks and `detect-private-key` run against every staged commit before it reaches GitHub. Config lives at `.pre-commit-config.yaml`; CI runs the same scans server-side via the `gitleaks` job in `.github/workflows/ci.yml`.

### Dependency lockfile

Phase 18a (2026-05-16) introduced a pinned lockfile via `pip-compile`. `requirements.in` carries the 16 direct deps with their loose `>=` constraints; `requirements.txt` is the regenerated lockfile that pins every direct and transitive package to an exact version. The lockfile shrinks the CVE-resolution window: when `pip-audit` flags a vulnerable transitive dep, the fix is a single deterministic `pip-compile` re-run rather than a guessing game about which transitive version users will resolve to at install time. The `lock-check` CI job rejects PRs where `requirements.txt` is out of sync with `requirements.in`, and Dependabot (configured in `.github/dependabot.yml`) opens weekly PRs against `requirements.in` so direct-dep upgrades and lockfile regeneration stay coupled to the existing `pip-audit` + `gitleaks` + CodeQL workflow.

### Rolling the headless-surface secrets

- **`TFGEN_API_KEY`**: generate a new random string (`openssl rand -hex 32`), update the Vercel env var, redeploy. Notify every script / Slack/JIRA bot that holds the old value. Old key stops working the moment the new value is bound; rotate during a low-traffic window.
- **`SLACK_SIGNING_SECRET`**: rotate from the Slack app's Basic Information page in the Slack admin console. Slack supports a 24-hour overlap where both secrets verify; copy the new secret to Vercel before the overlap ends.
- **`JIRA_WEBHOOK_SECRET`**: shared secret you control end-to-end. Update the JIRA Automation rule (or front proxy) and the Vercel env var in the same window. Old signatures fail validation immediately, so set both sides simultaneously.
- **`JIRA_API_TOKEN`**: regenerate at id.atlassian.com → Security → API tokens. Old tokens are revoked instantly; expect a brief callback failure if the JIRA bot account's old token was in use mid-flight.
- **`JAMF_CLIENT_ID` / `JAMF_CLIENT_SECRET`**: rotate from JAMF Pro Settings → System → API Roles and Clients. Generate a new client secret on the existing client (preserves the role binding); update Streamlit Cloud secrets, then revoke the old secret. Roles needed for live env-context: read on Policies, Computer Groups, Scripts, Packages, and Computer Extension Attributes.
- **`SNOWFLAKE_PRIVATE_KEY` (+ optional `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE`)**: rotate by generating a new RSA key-pair locally (`openssl genrsa -out new.pem 2048` then `openssl rsa -in new.pem -pubout -out new.pub`). Register the new public key on the Snowflake user (`ALTER USER <user> SET RSA_PUBLIC_KEY_2 = '<new-key-body>'` — the provider supports a primary + secondary key for zero-downtime rotation). Update the Streamlit Cloud secret value. Apply terraform once with the new key to confirm. Then promote the secondary to primary in Snowflake (`ALTER USER <user> SET RSA_PUBLIC_KEY = '<new-key-body>'; UNSET RSA_PUBLIC_KEY_2;`) and retire the old key. Snowflake forced key-pair auth as of November 2025; password rotation does NOT apply to Snowflake.
- **`SNOWFLAKE_ACCOUNT` / `SNOWFLAKE_USER` / `SNOWFLAKE_ROLE` / `SNOWFLAKE_WAREHOUSE`**: these are identifier-style env vars, not credentials. Rotate when the underlying Snowflake account/user/role/warehouse name changes (rare). The 90-day cadence applies because they're co-located with `SNOWFLAKE_PRIVATE_KEY` in the secrets file; refreshing the file naturally re-asserts the values.
- **`FLEET_URL` / `FLEET_API_TOKEN`**: rotate from Fleet UI → Account → API token. The token has the same scope as the user that owns it; for least-privilege, mint the token from a dedicated automation user with Observer or Maintainer role (not GitOps Admin) for read-mostly workflows. Both GitOps (`fleetctl apply --dry-run`) and the experimental Terraform path (`l-teles/fleetdm` provider) consume the same env vars; rotate both together. Old tokens stop working immediately; the rotation window should overlap by a few minutes via two valid tokens to bridge active apply runs.

## Headless surfaces (CLI, HTTP, Slack, JIRA)

Phase 10 Track 2 added four entry points that share the same `core.service.generate()` pipeline and the same `audit.py` + `cost.py` storage as the Streamlit app. Differences worth flagging for an IT review:

- **No Google OAuth gate**: the Streamlit `[auth]` block does not apply to CLI / HTTP / Slack / JIRA. Each surface has its own auth (env var, shared header secret, signing secret, HMAC) summarized in the README. Treat each surface as a separate authorization plane that needs its own access review.
- **PII redaction is preserved**: every surface calls `redact.redact()` before the prompt reaches Anthropic. Same patterns as the Streamlit app; same audit-log entries with per-category counts.
- **Quota model is per-actor when per-key tokens are configured** (Phase 21b). Each `[api_keys]` entry in `roles.toml` carries its own `daily_quota_usd`. Legacy `TFGEN_API_KEY` callers fall back to the env-var `TFGEN_HTTP_DAILY_QUOTA_USD` cap (default $5/day) which still acts as a shared ceiling across all Slack / JIRA actors that have not been issued per-key tokens.
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

## Phase 21: Tier 2 hardening bundle (2026-05-18)

### Per-key API tokens (Phase 21a)

The HTTP API now supports per-key tokens via the `[api_keys]` table in `roles.toml`. Each entry maps a SHA-256 hex of the issued plaintext token to:

- `actor_id`: operator-assigned stable identifier used as the audit/cost key.
- `role`: one of `viewer | contributor | editor | admin` (mirrors the Streamlit RBAC roles).
- `daily_quota_usd`: per-key cap. `0` means unlimited.
- `issued_at`: ISO-8601 UTC timestamp recorded by the issuance helper.

Plaintext tokens are NEVER stored on disk. They live in `roles.toml` only as SHA-256 hex. Comparison is done in constant time via `hmac.compare_digest` so a timing oracle cannot enumerate a token byte-by-byte.

Headers accepted (either works):

- `X-API-Key: <token>` (preferred; matches the pre-Phase-21 surface).
- `Authorization: Bearer <token>` (matches the Phase 21 spec language).

#### Issuing a new token

```text
python -m audit_github_sink issue \
    --actor-id service-account-1 \
    --role contributor \
    --quota-usd 2.00 \
    --note "Backstage CI pipeline"
```

The helper generates a fresh `secrets.token_urlsafe(32)` token, computes its SHA-256, appends a new `[api_keys.<slug>]` block to `roles.toml`, prints the plaintext token ONCE with a "store this now, it will not be shown again" notice, and logs an `api_key_issued` audit event carrying the actor_id, role, quota, and the first 8 hex characters of the hash (never the plaintext).

#### Deprecation of `TFGEN_API_KEY`

The legacy single shared secret `TFGEN_API_KEY` env var is still accepted for backwards compatibility. Requests matching the legacy value are tagged with the synthetic `actor_id = "legacy-tfgen"`; their daily cap falls back to `TFGEN_HTTP_DAILY_QUOTA_USD` or `[cost] daily_cap_usd` in `roles.toml`. New deployments should migrate to per-key tokens via the issuance helper. The legacy path will be removed in a future phase once all known callers have rotated.

### GitHub-backed audit sink (Phase 21c)

`audit_github_sink.py` is an opt-in mirror of every `audit.log()` call to a monthly JSONL file in the configured GitHub repo. Defence in depth: the existing local-file path and the per-user GitHub log keep writing independently, so a sink failure cannot lose events. Failures (429, 5xx, 404 on the audit-repo root, network errors) buffer events in memory and retry on the next call; after 10 consecutive failures the buffered batch is dropped with an `st.error` notification (Streamlit context) or a `print` to stderr (headless context). User-facing operations are NEVER blocked by a sink failure.

#### Storage layout

```text
_tftool/audit/audit-2026-05.jsonl
_tftool/audit/audit-2026-06.jsonl
...
```

The current month's file is read-modify-written. Older months are never modified.

#### Event shape

```json
{
  "timestamp": "2026-05-18T14:32:01Z",
  "actor_id": "service-account-1",
  "action": "rate_limited_http",
  "extra": {"surface": "http", "client_ip": "203.0.113.10", "retry_after_seconds": 30},
  "tf_tool_version": "06a7d12"
}
```

#### Configuration (in `roles.toml`)

```toml
[audit]
github_sink_enabled      = false   # default; flip to true to enable
github_audit_repo        = "owner/repo"
github_audit_path_prefix = "_tftool/audit/"
```

The sink authenticates via the same `GITHUB_TOKEN` already used by the per-user audit log and cost tracker. PAT scopes required: `repo` (write access to the audit repo).

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
