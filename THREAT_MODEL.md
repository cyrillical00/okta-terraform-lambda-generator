> This file is the canonical source. SECURITY.md describes the steady-state posture; THREAT_MODEL.md / INCIDENT_RESPONSE.md describe risks and the broken-glass procedures.

# Threat model

Last reviewed: 2026-05-16. Quarterly cadence; next review due 2026-08-16.

This document maps the assets the TF Tool protects, the trust boundaries between us and the systems we depend on, the threat actors we model against, and the ranked risk register. It is intentionally lighter than a formal STRIDE walkthrough; the goal is to make the highest-likelihood, highest-impact risks visible to a reviewer in one read.

See `SECURITY.md` for the steady-state posture (auth, RBAC, redaction, rotation cadence). This file does not re-document those controls; it references them and focuses on what could go wrong.

## 1. Assets

The TF Tool sits between human users, Anthropic's LLM, six provider APIs, and a target GitHub repo. The assets worth protecting, in rough order of blast radius:

### A1. Anthropic API key (highest blast radius)
`ANTHROPIC_API_KEY` is the single most expensive item to lose. A leak gives the attacker:
- Billing exposure on our Anthropic account (rate-limited but not capped by Anthropic at the key level).
- The ability to use Claude under our identity, which can taint our usage-pattern fingerprint with Anthropic abuse-detection.
- Indirect exposure of every prompt routed through the leaked key, since prompts sent through the leaked key reach Anthropic under our account.

### A2. Six provider API tokens (one per provider; each gates real customer infrastructure)
| Token | Gates |
|---|---|
| `OKTA_API_TOKEN` | Read+write on customer's Okta org (groups, apps, policies, users) |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Read on Lambda, IAM, CloudWatch; scope depends on IAM policy attached to the key |
| `GCP_SA_JSON` | Read on Cloud Functions / Run / Pub/Sub; scope depends on the service account's IAM role |
| `JAMF_CLIENT_ID` / `JAMF_CLIENT_SECRET` | Read on Policies, Computer Groups, Scripts, Packages, Extension Attributes |
| `FLEET_API_TOKEN` | Same scope as the Fleet user that minted it; Observer or Maintainer recommended |
| `SNOWFLAKE_PRIVATE_KEY` | Key-pair auth into the configured Snowflake user; warehouse / database / role access depends on the user's grants |

Loss of any one of these gives an attacker live read access (and write, in the Okta case) on real customer infrastructure. Okta is the highest single-provider risk because the token is read+write by default.

### A3. GitHub PAT
`GITHUB_TOKEN` carries `repo` write scope on the configured `GITHUB_REPO`. A leak gives push access to whatever org+repo the PAT is bound to. Audit logs also live in this repo, so a sufficiently motivated attacker can both push generated code AND tamper with the audit trail.

### A4. User-supplied prompts
Free-text prompts may contain PII (employee emails, phone numbers), business logic (group names, app shapes that reveal HR org structure), and inadvertently-pasted secrets. `redact.py` strips a known set of patterns before the prompt leaves Streamlit; see `SECURITY.md` for the list. The redacted prompt is logged in `audit.jsonl` as the first 200 characters.

### A5. Generated Terraform / YAML
Output files may end up in the customer's apply pipeline. A hallucinated or prompt-injected secret in the output goes straight into a Terraform repo and (worst case) gets `terraform apply`'d into a real provider. Validation passes catch syntactic errors but do not pattern-match on secret shapes.

### A6. Audit log
`_tftool/audit/<email-hash>.jsonl` in the GitHub repo. Forensic record of who-did-what-when. Append-only by the application, but the file format itself is not append-only at the filesystem level. Anyone with push access to the repo can rewrite history.

## 2. Trust boundaries

```
+--------------------+
| User's browser     |
+--------+-----------+
         | TLS (Streamlit Cloud certificate)
         v
+--------+-----------+
| Streamlit Cloud    |   <-- managed by Streamlit; we do not control the host OS, the K8s layer, or the secret manager backend
| (US region)        |
+--+-----+-----+-----+
   |     |     |
   |     |     +----> Anthropic API (TLS)                <-- prompts + parsed intent leave our trust boundary
   |     |
   |     +----------> Provider APIs (Okta, AWS, GCP,     <-- one trust boundary per provider; live env-context reads
   |                  JAMF, Fleet, Snowflake)
   |
   +----------------> GitHub API (TLS)                   <-- push generated files + audit log; auth via PAT, not OAuth
```

Out-of-band boundaries:
- **Local dev machines**: developers (currently 1) hold `.streamlit/secrets.toml` on disk. Treat each laptop as a credential vault.
- **Headless surfaces (HTTP / Slack / JIRA)**: each surface terminates at the same FastAPI app on Vercel, with its own auth gate. The Vercel deploy holds the same secrets as Streamlit Cloud (two copies of the credential surface).
- **Status page** (`status.olegstrutsovski.com`): static; no credentials; out of trust scope.

## 3. Threat actors

Modelled lightly, not as a full STRIDE pass. Each row is "who they are, what they can do today, what they can't."

### T1. Anonymous internet attacker
- **Can do today**: nothing on the Streamlit app (Google OAuth gate blocks every route). Can hit `/api/health` on the Vercel deploy (returns a 200 with no data). Can attempt to POST `/api/generate` without `X-API-Key` (returns 401).
- **Cannot do**: reach any generation or push code path. The auth gate is on every protected route.
- **Mitigation status**: closed unless OAuth or the API-key check has a bypass bug.

### T2. Authenticated user, viewer role
- **Can do today**: parse intent (read-only LLM call against their own daily quota of $0.50). View their own audit / cost.
- **Cannot do**: generate, push, manage roles, see other users' data.
- **Residual risk**: a viewer can still send prompts that contain PII to Anthropic. `redact.py` covers the known patterns; rare shapes (e.g. EU phone numbers, international SSN-equivalents) slip through.

### T3. Authenticated user, editor / contributor role
- **Can do today**: full generation pipeline + push. Editor can push to any repo configured under `GITHUB_REPO`; contributor can only push to repos owned by their own GitHub username.
- **Residual risk**: a malicious editor can push generated content with embedded prompt-injected payloads. The branch protection rule on the receiving repo is the only gate before the code merges to main.

### T4. Authenticated user, admin role
- **Can do today**: everything T3 can, plus manage `roles.toml`, see every user's audit log, disable PII redaction per session.
- **Residual risk**: high. Admin is functionally root. The audit log captures the redaction-toggle event, but a malicious admin who disables redaction can leak PII into prompts at will.
- **Mitigation status**: admin role assignment is manual via Streamlit Cloud Secrets; the bar to becoming admin is the bar to editing the cloud secrets file.

### T5. Malicious dependency author (supply-chain)
- **Can do today**: ship a compromised version of any pinned dependency. `streamlit`, `anthropic`, `pyyaml`, `pydantic`, `fastapi`, `python-multipart`, `requests`, `google-cloud-*`, `boto3`, `okta-sdk-python`. Anyone of these gets executed in the Streamlit Cloud runtime with our secrets in process memory.
- **Mitigation status**: partially covered. Dependabot + pip-audit (Phase 17a) catch known CVEs after publication. The window between a malicious upload and detection (typo-squat, repo takeover) is uncovered.

### T6. Anthropic insider / breach
- **Can do today**: read every prompt we've ever sent. Prompts include intent JSON, which often names internal groups, apps, customer environments.
- **Mitigation status**: not under our control. PII redaction limits the exposure but not the business-context exposure. Anthropic's posture is the residual.

### T7. Streamlit Cloud insider / breach
- **Can do today**: read our entire secret manager (six provider tokens + GitHub PAT + Anthropic key). Read session state in memory.
- **Mitigation status**: not under our control. Single-tenant per-customer Streamlit Cloud apps limit blast radius to one customer's secrets per breach.

### T8. Provider API insider (e.g. Okta or AWS employee)
- **Can do today**: misuse a customer's provider account directly. Bypasses our trust path entirely; not really our threat to model, but worth naming because it bounds our claims about provider data.

### T9. Lost / compromised admin Google account
- **Can do today**: sign in to the Streamlit app as a real admin, full access to everything T4 has. Org-level MFA (Google Workspace) is the only gate.
- **Residual risk**: high. We do not currently require app-level step-up auth for `manage_roles` or "disable redaction" actions; a session-hijacked admin can do real damage in seconds.

## 4. Top risks (ranked by likelihood x impact)

Each risk follows the same shape: scenario, current mitigation, residual risk, future mitigation. Ordered roughly highest-priority first.

### R1. Anthropic key leak via repo commit
**Scenario**: Developer accidentally commits `.streamlit/secrets.toml` or pastes the key into a code snippet that lands in a public PR or a public gist.

**Current mitigation**:
- `.gitignore` lists `.streamlit/secrets.toml`.
- Gitleaks pre-commit hook + CI scan (Phase 17a, landing in parallel).
- CodeQL scans on push (Phase 17a).

**Residual risk**: Developer machine compromise (key sits on disk in plaintext); leak via a non-git channel (Slack paste, screenshot, screenshare).

**Future mitigation**: Short-lived key tokens via Anthropic OAuth when available. Until then, rotating the key on the 90-day cadence is the operational mitigation.

### R2. Generated HCL contains a secret (LLM hallucination or user-supplied)
**Scenario**: The LLM emits a literal string that looks like an API key in the generated Terraform, or a user prompt includes a secret that survives redaction (e.g. an internal service token without a recognizable prefix).

**Current mitigation**:
- `redact.py` strips the known secret shapes before prompts leave Streamlit (Anthropic / OpenAI / Stripe / GitHub PAT / AWS access key / JWT).
- Generator prompt rules explicitly tell the model not to emit literal secrets.
- Terraform validate pass catches some syntactic errors but does not regex-scan output for secret shapes.

**Residual risk**: redaction-pattern gap (unrecognized custom secret prefix, EU formats, non-Luhn-passing card numbers). LLM hallucination is rare but possible.

**Future mitigation**: Post-generation secret-shape scan on the output (run the same redaction patterns over the generated HCL, fail-closed if any match). Cheap to add; deferred until a real incident proves the need.

### R3. Admin Google account takeover
**Scenario**: Admin's Google account is phished or session-hijacked; attacker signs in to the Streamlit app as admin and either disables redaction, exfiltrates audit logs, or modifies `roles.toml` to grant themselves persistent access.

**Current mitigation**:
- Org-level MFA enforcement on Google Workspace (org admin's responsibility).
- 30-minute idle session timeout limits the window of an unattended hijacked session.
- Audit log captures every privilege-change event.

**Residual risk**: Depends entirely on the org admin's MFA discipline. A real-time phish that captures both password + TOTP defeats this.

**Future mitigation**: App-level step-up auth for `manage_roles` and "disable redaction" actions (re-prompt for Google reauth at the action boundary). Deferred.

### R4. Provider token rotation lag (90-day cadence missed)
**Scenario**: One or more provider tokens age past their target rotation cadence; if any of them is leaked (R1 / R2), the blast-radius window is longer than the cadence implies.

**Current mitigation**:
- `secret_rotation.py` tracker writes to `_tftool/secret_rotation.json`.
- Admin sidebar widget shows a warning when any tracked secret is older than its target cadence (90 / 180 days per `SECURITY.md`).

**Residual risk**: Human-in-loop. The widget warns; nothing forces rotation. An admin who dismisses the warning has zero further reminders.

**Future mitigation**: Automated rotation (mint new token, swap secret, revoke old token) per provider. Per-provider work; deferred until the operational cost of manual rotation exceeds the build cost of automation.

### R5. Shared `TFGEN_HTTP_DAILY_QUOTA_USD` exhausted by one bad actor
**Scenario**: HTTP / Slack / JIRA all share one daily $5 cap. A single misbehaving API-key caller, Slack user, or JIRA bot drains the cap; legitimate users on the same surface are blocked until next UTC midnight.

**Current mitigation**:
- Default cap is $5/day across all headless surfaces.
- Quota is enforced before each generation call, so the cap prevents unbounded billing.

**Residual risk**: Noisy-neighbour DoS within a single day. No way to identify the offender from the quota state alone (the audit log captures hashed identifiers).

**Future mitigation**: Per-actor quotas via a `[api_keys]` table in `roles.toml`. Deferred until traffic justifies the build.

### R6. Dependency CVE lands in master via transitive upgrade
**Scenario**: A pinned dependency releases a new version with a fresh CVE; pip-audit / Dependabot opens a PR; merge happens before the world knows about the CVE.

**Current mitigation**:
- Dependabot version-bump PRs (Phase 17a).
- pip-audit on CI (Phase 17a) flags known-vulnerable versions at PR time.

**Residual risk**: 0-day window between vulnerability disclosure and pip-audit's database update. Also: transitive deps not directly pinned can drift on `pip install -r requirements.txt`.

**Future mitigation**: Lockfile (`requirements.lock` via `pip-compile`) to freeze the full transitive graph. Deferred; would require a rebuild of the CI pipeline.

### R7. Audit log tampering
**Scenario**: Anyone with push access to `GITHUB_REPO` can rewrite `_tftool/audit/<email-hash>.jsonl` files via direct commit, erasing forensic history.

**Current mitigation**: Application writes append-only; never edits or deletes records. Branch protection on `GITHUB_REPO` would limit who can push, but is not enforced by this tool.

**Residual risk**: Anyone with shell + git access can rewrite. The audit trail is best-effort, not tamper-evident.

**Future mitigation**: Remote append-only sink (e.g. S3 with object lock, or a write-only audit endpoint that fronts a tamper-evident store). Deferred.

### R8. Streamlit Cloud breach exposes our secrets
**Scenario**: Streamlit Cloud's secret manager is compromised; an attacker reads our full secret set in one shot. Blast radius is all six providers + Anthropic + GitHub.

**Current mitigation**:
- Rotation cadence limits the exposure window per token.
- Single-tenant per-customer deployment limits the blast radius to one customer's secrets per app.
- Streamlit Cloud's own controls (we trust their posture).

**Residual risk**: Catastrophic if it happens. The full credential surface is compromised in one event; the incident response runbook (`INCIDENT_RESPONSE.md`) rotates everything.

**Future mitigation**: Vault / KMS-backed runtime secrets (HashiCorp Vault, AWS Secrets Manager, Google Secret Manager) so Streamlit Cloud only holds a short-lived token to the real vault. Major rebuild; deferred until customer demand or a real incident.

## 5. Out of scope (explicitly)

The following are not modelled in this threat model and not mitigated by this tool. Reviewers should treat them as the customer's or upstream provider's responsibility.

- **DDoS protection**: Streamlit Cloud's responsibility. We do not have a CDN, WAF, or rate-limiting layer in front of the app. The Vercel HTTP deploy has Vercel's default DDoS protection.
- **Provider-side vulnerabilities**: we trust the providers (Okta, AWS, GCP, JAMF, Fleet, Snowflake, Anthropic, GitHub, Streamlit). A flaw in any of them is an upstream incident, not ours.
- **Quantum-era cryptography**: TLS / RSA in the current crypto era is assumed sound.
- **Physical security**: laptops, datacenters, the Streamlit Cloud underlying hosts. Not in scope.
- **SAML / SCIM**: explicitly out of build per `SECURITY.md`. Customers requiring SAML deploy single-tenant behind their own SAML proxy.
- **Multi-tenant org isolation within one app instance**: we ship one Streamlit Cloud app per customer; cross-customer isolation is not engineered.
- **Customer-managed encryption keys**: Streamlit Cloud uses its underlying storage's encryption keys. Not customer-managed.
- **EU / non-US region deployment**: US-only today.
- **SOC2 Type 2 attestation, SLA framework, DPA**: explicitly deferred.

## 6. Update cadence

Review quarterly. Each review should answer:

1. Has the asset list changed? (New provider, new env var, new output mode that touches new data.)
2. Has the trust boundary diagram changed? (New external dependency, new deploy target.)
3. Has the threat actor list changed? (New role, new headless surface, new auth model.)
4. Has any R-ranked risk changed in likelihood or impact? (Did a mitigation land, or did a residual risk grow?)
5. Are any "deferred" mitigations now justified by traffic, customer demand, or a real incident?
6. Are any "out of scope" items now in scope?

| Review date | Reviewer | Changes | Next review |
|---|---|---|---|
| 2026-05-16 | Oleg Strutsovski | Initial draft (Phase 17c). Six providers, five entry points, eight ranked risks, nine threat actors. | 2026-08-16 |

Append a row per review. When a risk is closed (mitigation lands), keep the row in the table but note "closed in <commit-sha>" in the residual-risk line.
