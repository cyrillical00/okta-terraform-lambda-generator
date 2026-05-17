> This file is the canonical source. SECURITY.md describes the steady-state posture; THREAT_MODEL.md / INCIDENT_RESPONSE.md describe risks and the broken-glass procedures.

# Incident response

Last reviewed: 2026-05-16. Quarterly cadence; next review due 2026-08-16.

This document is the operational runbook for security incidents in the TF Tool. It pairs with `THREAT_MODEL.md` (what could go wrong) and `SECURITY.md` (steady-state posture). Every command in this file is copy-pasteable. If you find yourself reading prose where a command should be, that is a bug; file an issue.

## 1. Severity levels

| Severity | Definition | Examples | Response time |
|---|---|---|---|
| sev1 | Actively losing money, data, or trust. Credentials confirmed exposed; provider account compromised; data exfiltration in progress. | Anthropic key in a public commit. Okta token in a Slack channel. Admin account session hijacked. | Stop everything; rotate within 1 hour. |
| sev2 | Degraded function or material risk, but no active loss. Credentials suspected exposed; CVE published in a pinned dep; rotation overdue. | pip-audit reports a CVE in `requests`. Secret rotation 30 days overdue. Audit log gap detected. | Begin response within 1 business day. |
| sev3 | Fix-when-convenient. Hardening gap; pattern discovered that could become an issue; non-exploitable. | Redaction pattern misses an edge-case format. Out-of-cadence pip-audit warning on a dev-only dep. Documentation drift between SECURITY.md and reality. | Track on a backlog issue; close within the next planned phase. |

Default to one level higher than your gut tells you. It is cheaper to over-rotate than to under-rotate.

## 2. Broken-glass procedures

One numbered runbook per high-likelihood scenario. Run them in order; do not skip steps. Each runbook ends with "post-incident" steps that must happen even if the rotation succeeded.

### IR-1: Anthropic API key leaked (sev1)

Symptom: key visible in a commit, gist, Slack message, screenshot, screenshare recording, OR Anthropic console usage spike on a non-working hour.

```text
Step 1. Revoke at https://console.anthropic.com/settings/keys
        Click the leaked key, then "Revoke". This is instantaneous.

Step 2. Mint a replacement at https://console.anthropic.com/settings/keys
        Click "Create Key". Name it with today's date, e.g. "tf-tool-2026-05-16".
        Copy the new value into your clipboard immediately; the console only
        shows it once.

Step 3. Update Streamlit Cloud Secrets UI
        https://share.streamlit.io -> your app -> Settings -> Secrets
        Find ANTHROPIC_API_KEY, paste new value, click Save, then Reboot app.

Step 4. Update local dev machines
        Open .streamlit/secrets.toml on every machine that has it:
        # Windows
        notepad C:\Users\cbot\TF Tool\.streamlit\secrets.toml
        # macOS / Linux
        $EDITOR ~/path/to/TF\ Tool/.streamlit/secrets.toml
        Replace ANTHROPIC_API_KEY value. Save.

Step 5. Update Vercel env (if HTTP / Slack / JIRA surfaces are deployed)
        vercel env rm ANTHROPIC_API_KEY production
        vercel env add ANTHROPIC_API_KEY production
        # paste new value when prompted
        vercel --prod   # redeploy

Step 6. Update _tftool/secret_rotation.json
        Edit the ANTHROPIC_API_KEY row, set last_rotated to today's date in
        YYYY-MM-DD format. Commit and push.

Step 7. Review for unauthorized usage during exposure window
        https://console.anthropic.com/settings/usage
        Check usage between the leak timestamp and the revocation timestamp.
        Anomalous spikes are the signal. Document the findings.

Step 8. Post-incident
        - Investigate leak source (commit log, Slack search, screenshare review).
        - File a memory under ~/.claude/projects/.../memory/feedback_*.md so the
          pattern is in muscle memory next time.
        - Open a post-mortem doc using the template in section 4.
```

### IR-2: GitHub PAT leaked (sev1)

```text
Step 1. Revoke at https://github.com/settings/tokens
        Find the leaked PAT, click "Delete". Instantaneous.

Step 2. Mint a replacement at https://github.com/settings/tokens/new
        Scope: repo (write). Expiration: 90 days. Name it with today's date.

Step 3. Update Streamlit Cloud Secrets UI
        GITHUB_TOKEN -> new value -> Save -> Reboot app.

Step 4. Update local .streamlit/secrets.toml on dev machines.

Step 5. Update Vercel env
        vercel env rm GITHUB_TOKEN production
        vercel env add GITHUB_TOKEN production
        vercel --prod

Step 6. Update _tftool/secret_rotation.json (GITHUB_TOKEN row).

Step 7. Review repo audit log
        https://github.com/<owner>/<repo>/settings/audit-log
        Look for pushes during the exposure window that did not originate
        from a known actor. Check force-pushes and branch deletions.

Step 8. Post-incident: as IR-1 step 8.
```

### IR-3: JAMF Pro credentials leaked (sev1)

```text
Step 1. Revoke at https://<your-tenant>.jamfcloud.com -> Settings -> System
        -> API Roles and Clients
        Find the leaked client, click "Disable" or rotate the client secret.

Step 2. If rotating the secret only (preserves role binding):
        On the existing client, click "Generate new client secret".
        Copy the new value.
        If creating a new client: assign the same role (read on Policies,
        Computer Groups, Scripts, Packages, Computer Extension Attributes).

Step 3. Update Streamlit Cloud Secrets UI
        JAMF_CLIENT_ID and JAMF_CLIENT_SECRET -> new values -> Save -> Reboot.

Step 4. Update local .streamlit/secrets.toml on dev machines.

Step 5. Update Vercel env (if applicable).

Step 6. Update _tftool/secret_rotation.json (JAMF_CLIENT_ID/SECRET row).

Step 7. Review JAMF audit logs
        https://<your-tenant>.jamfcloud.com -> Settings -> System -> Change
        Management
        Look for API actions during the exposure window that did not match
        expected automation.

Step 8. Post-incident: as IR-1 step 8.
```

### IR-4: Fleet API token leaked (sev1)

```text
Step 1. Revoke at https://<your-fleet-host>/dashboard -> Account -> API token
        Click "Get new token". The old token is invalidated immediately.

Step 2. The new token has the same scope as the user that minted it. For
        least-privilege, mint from a dedicated automation user with Observer
        or Maintainer role, not GitOps Admin.

Step 3. Update Streamlit Cloud Secrets UI
        FLEET_API_TOKEN -> new value -> Save -> Reboot.
        Keep FLEET_URL value if the host has not changed.

Step 4. Update local .streamlit/secrets.toml on dev machines.

Step 5. Update Vercel env (if applicable).

Step 6. Update _tftool/secret_rotation.json (FLEET row).

Step 7. Review Fleet activity log
        https://<your-fleet-host>/dashboard/activity
        Look for API actions during the exposure window.

Step 8. Post-incident: as IR-1 step 8.
```

### IR-5: Snowflake key-pair compromised (sev1)

Per `SECURITY.md`, Snowflake forced key-pair auth in Nov 2025. There is no password to rotate; the private key is the credential.

```text
Step 1. Generate a new RSA key-pair locally
        openssl genrsa -out new_private.pem 2048
        openssl rsa -in new_private.pem -pubout -out new_public.pem

Step 2. Read the new public key body (strip BEGIN/END headers)
        # macOS / Linux
        cat new_public.pem | sed '1d;$d' | tr -d '\n'
        # Windows PowerShell
        (Get-Content new_public.pem | Select-Object -Skip 1 | Select-Object -SkipLast 1) -join ""

Step 3. Register the new key as the SECONDARY (zero-downtime rotation)
        Connect to Snowflake as a role that can ALTER USER.
        Run:
          ALTER USER <user> SET RSA_PUBLIC_KEY_2 = '<new-key-body>';

Step 4. Update Streamlit Cloud Secrets UI
        SNOWFLAKE_PRIVATE_KEY -> paste the new private key value
        (including the -----BEGIN PRIVATE KEY----- and -----END PRIVATE KEY-----
        lines, with literal newlines).
        Save -> Reboot.

Step 5. Apply terraform once with the new key to confirm it works.
        terraform init
        terraform plan
        Expect: no auth error.

Step 6. Promote secondary to primary; retire old key
        ALTER USER <user> SET RSA_PUBLIC_KEY = '<new-key-body>';
        ALTER USER <user> UNSET RSA_PUBLIC_KEY_2;

Step 7. Update local .streamlit/secrets.toml on dev machines.

Step 8. Update Vercel env (if applicable).

Step 9. Update _tftool/secret_rotation.json (SNOWFLAKE row).

Step 10. Review Snowflake query history for unauthorized usage
         SELECT user_name, query_text, start_time
         FROM snowflake.account_usage.query_history
         WHERE user_name = '<user>'
           AND start_time BETWEEN '<leak-ts>' AND '<revoke-ts>';

Step 11. Post-incident: as IR-1 step 8.
```

### IR-6: Okta API token leaked (sev1)

```text
Step 1. Revoke at https://<your-org>-admin.okta.com -> Security -> API
        -> Tokens
        Find the leaked token, click "Revoke". Instantaneous.

Step 2. Mint a replacement: Security -> API -> Tokens -> Create Token
        Name it with today's date. Copy the value immediately.

Step 3. Update Streamlit Cloud Secrets UI
        OKTA_API_TOKEN -> new value -> Save -> Reboot.

Step 4. Update local .streamlit/secrets.toml on dev machines.

Step 5. Update Vercel env (if applicable).

Step 6. Update _tftool/secret_rotation.json (OKTA row).

Step 7. Review Okta System Log for unauthorized usage
        https://<your-org>-admin.okta.com -> Reports -> System Log
        Filter to "API requests" during the exposure window. Look for
        actions that did not originate from a known automation IP.

Step 8. Post-incident: as IR-1 step 8.
```

### IR-7: AWS access key leaked (sev1)

```text
Step 1. Deactivate at https://console.aws.amazon.com/iam/home -> Users
        -> <user> -> Security credentials
        Click "Make inactive" on the leaked access key. Instantaneous.

Step 2. Mint a replacement: same screen -> "Create access key".
        Copy both Access key ID and Secret access key immediately.

Step 3. Update Streamlit Cloud Secrets UI
        AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY -> new values
        Save -> Reboot.

Step 4. Update local .streamlit/secrets.toml on dev machines.

Step 5. Update Vercel env (if applicable).

Step 6. Update _tftool/secret_rotation.json (AWS row).

Step 7. Delete the old (now-inactive) key from the IAM console.
        Do not leave it as "inactive" forever; delete it.

Step 8. Review CloudTrail for unauthorized usage
        https://console.aws.amazon.com/cloudtrail/home
        Filter to the IAM user, time window = exposure window. Look for
        API calls outside expected automation patterns.

Step 9. Post-incident: as IR-1 step 8.
```

### IR-8: GCP service account key leaked (sev1)

```text
Step 1. Delete at https://console.cloud.google.com/iam-admin/serviceaccounts
        Click the service account -> Keys tab -> Delete the leaked key.
        Instantaneous.

Step 2. Mint a replacement: same screen -> Add Key -> Create new key
        -> JSON. Download the JSON file.

Step 3. Convert the JSON to a single line for the secrets file
        # macOS / Linux
        cat ~/Downloads/key.json | python -c "import sys, json; print(json.dumps(json.load(sys.stdin)))"
        # Windows PowerShell
        (Get-Content C:\Users\cbot\Downloads\key.json -Raw | ConvertFrom-Json | ConvertTo-Json -Compress)

Step 4. Update Streamlit Cloud Secrets UI
        GCP_SA_JSON -> paste single-line JSON -> Save -> Reboot.

Step 5. Update local .streamlit/secrets.toml on dev machines.

Step 6. Update Vercel env (if applicable).

Step 7. Update _tftool/secret_rotation.json (GCP_SA_JSON row).

Step 8. Review GCP audit logs for unauthorized usage
        https://console.cloud.google.com/logs/query
        Filter:
          protoPayload.authenticationInfo.principalEmail="<sa-email>"
          timestamp >= "<leak-ts>" AND timestamp <= "<revoke-ts>"
        Look for actions outside expected automation patterns.

Step 9. Post-incident: as IR-1 step 8.
```

### IR-9: Admin Google account compromised (sev1)

This is the highest-severity scenario short of Streamlit Cloud breach. An attacker with a live admin session can disable redaction, exfiltrate audit logs, and modify `roles.toml`.

```text
Step 1. Revoke all sessions at https://myaccount.google.com/security
        Sign in as the compromised user (or have the org admin do it).
        Click "Manage devices" -> "Sign out" on every device that is not
        the legitimate user's current device.
        If unable to sign in: org admin in Google Workspace can force
        sign-out: admin.google.com -> Users -> <user> -> Sign out user.

Step 2. Reset the password
        myaccount.google.com -> Security -> Password
        Generate a new password (24+ characters, password manager).
        Verify MFA is enabled (Authenticator app, not SMS).

Step 3. Reauthenticate to Streamlit
        Visit the Streamlit app, sign out, sign back in with new password
        + MFA. Confirm Google OAuth still resolves to admin role.

Step 4. Audit recent admin actions
        Pull the audit log:
        # Clone the GITHUB_REPO if not already local
        gh repo clone <owner>/<repo>
        cd <repo>
        # Find the admin's email-hash file under _tftool/audit/
        ls _tftool/audit/
        # Inspect the last 50 entries
        tail -n 50 _tftool/audit/<email-hash>.jsonl
        Look for: manage_roles events, redaction toggle events, unfamiliar
        push events, generation events at unusual times.

Step 5. If audit log shows unauthorized actions, rotate every secret
        per IR-1 through IR-8. Treat as full credential surface compromise.

Step 6. Post-incident
        - Run the post-mortem template in section 4.
        - Add hardware key (Yubikey) requirement on the admin account.
        - Consider app-level step-up auth for manage_roles (deferred per
          THREAT_MODEL.md R3 future mitigation).
```

### IR-10: Generated HCL contains a secret (sev1 if applied; sev2 if caught pre-apply)

```text
Step 1. Identify the secret. Look at the generated file. What shape is it?
        - sk-ant-... -> follow IR-1
        - ghp_... or github_pat_... -> follow IR-2
        - AKIA... or ASIA... -> follow IR-7
        - okta-API-token shape (40 char base64-ish) -> follow IR-6
        - JAMF client secret (UUID-like) -> follow IR-3
        - Fleet token (long hex / base64) -> follow IR-4
        - Snowflake key (PEM-shaped) -> follow IR-5
        - GCP SA JSON (JSON blob) -> follow IR-8

Step 2. If the HCL was already applied to a real provider, treat as sev1
        and rotate per the IR-X above. If only generated but not pushed,
        treat as sev2 and:
        - delete the generated file locally
        - if pushed to GitHub, force-push history rewrite:
          git filter-repo --path <file> --invert-paths
          git push --force-with-lease
        - if applied to a provider, rotate the secret per IR-X.

Step 3. Investigate the source of the secret in the output
        Look at the audit log entry for the generation. The redacted prompt
        is in the first 200 chars. Was the secret in the user's prompt?
        Did redact.py miss it? Or did the LLM hallucinate it?

Step 4. If redact.py missed it:
        - Add the pattern to ui/redact.py (see Phase 17b)
        - Add a test case in tests/test_redact.py
        - File a sev3 follow-up to extend the post-gen secret-shape scan
          (R2 future mitigation in THREAT_MODEL.md).

Step 5. If the LLM hallucinated it:
        - Add a "never emit literal secrets" reinforcement to the relevant
          SECTION in generator/prompts.py
        - Add a QA case in qa_runner.py that asserts no secret-shape regex
          matches in the output.

Step 6. Post-incident: as IR-1 step 8.
```

## 3. Communication

Who to notify per severity. Keep this list short; over-notification trains people to ignore real incidents.

| Severity | Notify | Channel | When |
|---|---|---|---|
| sev1 | Owner (Oleg) + any active admin user of the affected app | Direct (phone if no response in 15 min) | Immediately, before starting the runbook |
| sev2 | Owner + admin users via in-app banner | Email + sidebar warning widget | Within 1 business day |
| sev3 | Backlog issue | GitHub issue with `security` label | Whenever; no real-time notification needed |

Post-mortem template (see section 4) is required for every sev1 and recommended for sev2. The doc lives in the same `GITHUB_REPO` under `_tftool/postmortems/<YYYY-MM-DD>-<short-name>.md`.

## 4. Post-incident review template

Copy this into a new markdown file. Fill in every question. Be specific.

```markdown
# Post-mortem: <short title>

Date: YYYY-MM-DD
Severity: sev1 | sev2 | sev3
Author: <name>
Status: draft | reviewed | closed

## 1. Timeline

- HH:MM TZ - first signal observed
- HH:MM TZ - paged / detected by <whom or what>
- HH:MM TZ - response started, ran <which runbook>
- HH:MM TZ - rotation complete / mitigation in place
- HH:MM TZ - post-incident verification complete
- HH:MM TZ - all-clear declared

## 2. Detection delay

How long between the actual incident and our detection? Why?

## 3. Root cause

What actually happened. Be specific. "Developer machine compromise" is
not specific. "Developer pasted the key into a Slack channel that was
later screenshotted by a contractor" is specific.

## 4. What we will change

Concrete actions, each with an owner and a due date. Format:
- [ ] action - owner - due YYYY-MM-DD

## 5. What we will NOT change, and why

Things we considered changing but decided against. Document the reasoning
so future incidents do not re-litigate the same trade-off.
```

When the post-mortem is complete, link it from `THREAT_MODEL.md` section 6 (review cadence table) under the next quarterly review row. Closed-loop on the threat model is the point.
