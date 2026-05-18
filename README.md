# Okta Terraform + Lambda + GCP Generator

A Streamlit app that turns plain-English infrastructure descriptions into deployable Terraform HCL across four providers. Output covers Okta resources, AWS Lambda glue (Okta event hooks calling Lambdas, scheduled sweeps), GCP Cloud Functions / Cloud Run / Pub/Sub, and JAMF Pro device-management resources (policies, smart groups, scripts, configuration profiles). One click pushes the generated files to GitHub; another saves a ZIP locally.

Live at https://okta-terraform-lambda-generator.streamlit.app. Status: https://status.olegstrutsovski.com.

## What it generates

Seven output modes selectable from the sidebar:

| Mode | Files |
|---|---|
| Okta Terraform only | `terraform/okta.tf` |
| Both | `terraform/okta.tf`, `terraform/lambda.tf`, `lambda/lambda_function.py`, `lambda/requirements.txt` |
| Lambda only | `terraform/lambda.tf`, `lambda/lambda_function.py`, `lambda/requirements.txt` |
| GCP only | `terraform/gcp.tf`, `cloud_function/main.py`, `cloud_function/requirements.txt` |
| Okta + GCP | `terraform/okta.tf`, `terraform/gcp.tf`, `cloud_function/main.py`, `cloud_function/requirements.txt` |
| JAMF only | `terraform/jamf.tf` |
| Okta + JAMF | `terraform/okta.tf`, `terraform/jamf.tf` |

Composite modes (Okta+AWS, Okta+GCP, Okta+JAMF) automatically merge `terraform { required_providers {} }` blocks and dedupe `variable "X" {}` declarations so the generated files coexist in a single Terraform module without duplicate-block errors.

### JAMF Pro

JAMF support targets `deploymenttheory/jamfpro` v0.37.x (community, public preview). Covers 12 of the provider's 74 resources: policies, scripts, macOS / mobile configuration profiles, smart computer groups (v2), static groups, packages, extension attributes, restricted software, and DEP prestage enrollment. Anything outside that list (live MDM commands, certificate signing, custom branding, the long tail) emits a `# NOTE` comment pointing to the JAMF Pro web console rather than fabricating non-existent HCL.

Two non-negotiable apply-time constraints baked into every JAMF output:
- `terraform apply -parallelism=1` is required (Jamf Pro produces inconsistent behaviour at the default parallelism of 10).
- `jamfpro_load_balancer_lock = true` must be set in the provider block for any Jamf Cloud target (`*.jamfcloud.com`) due to the 60-second LB cookie propagation delay.

Each generated `terraform_jamf_hcl` file ships with a top-of-file `# JAMF APPLY RUNBOOK` comment listing both constraints and the eventual-consistency warning so the apply runbook is self-documenting.

### Fleet MDM (GitOps YAML, recommended)

Fleet support targets the official `fleetctl` GitOps workflow. Output mode `Fleet GitOps only` (or composite `Okta + Fleet GitOps`) emits a single `fleet/default.yml` containing inline policies, labels, queries, agent options, controls, and software definitions, plus a `# FLEET GITOPS APPLY RUNBOOK` header documenting `fleetctl apply -f default.yml --dry-run` (validate) and `fleetctl apply -f default.yml` (apply). Required env: `FLEET_URL`, `FLEET_API_TOKEN`. Server requirement: Fleet >= 4.82.0.

YAML validation runs in `fleet_validate.py` (pure Python, PyYAML-based) — confirms top-level keys, required fields per resource, label mutual-exclusivity (`query` XOR `hosts` XOR `criteria`), and apply runbook header presence. An optional `fleetctl apply --dry-run` second pass runs when `fleetctl` is on `PATH`.

### Fleet MDM (Terraform, EXPERIMENTAL)

For shops that want Fleet declarations alongside their Okta / AWS Terraform, output modes `Fleet TF only` and `Okta + Fleet TF` emit HCL using the community-maintained `l-teles/fleetdm` provider, pinned to exactly `0.5.4`. The provider README explicitly says "USE AT YOUR OWN RISK" and notes it was "developed primarily through AI assistance". Every `terraform_fleet_hcl` output ships with a loud `# EXPERIMENTAL FLEET PROVIDER WARNING` block at the top so the risk is visible at apply time.

The GitOps YAML path (`Fleet GitOps only`) remains the recommended route for production use; the Terraform path is for IaC shops that want a single `terraform apply` driving Okta + AWS + Fleet together. The provider supports 14 resource types (12 in the active surface, plus the deprecated `fleetdm_team` / `fleetdm_query` aliases) against Fleet server 4.82.0+; Premium-only resources (`fleetdm_software_package`, `fleetdm_bootstrap_package`, `fleetdm_configuration_profile`, `fleetdm_setup_experience`) need a Fleet Premium licence at apply time and are tagged with a `# PREMIUM` marker in the generated HCL.

Provider block: `server_address` + `api_key` + `verify_tls = true`, fed from `var.fleetdm_url` and `var.fleetdm_api_key`. Apply-time env: `FLEETDM_URL`, `FLEETDM_API_TOKEN`. The live-context fetcher in `app.py` accepts both the legacy `FLEET_URL` / `FLEET_API_TOKEN` secret names (Phase 14) and the new `FLEETDM_*` names, so no secret rotation is required.

SECTION J of the system prompt is grounded directly in the provider's cached source (`_tftool/.terraform-plugin-cache/registry.terraform.io/l-teles/fleetdm/0.5.4/windows_amd64/README.md` + `CHANGELOG.md`), not a summarised registry fetch. The FT01-FT12 regression class in `qa_runner.py` exercises every resource type and asserts both correct attribute names and forbidden-string absence (no `fleetdm_team`, no `fleetdm_query`, no `url = ` / `api_token = ` from the pre-Phase-19a schemas).

### Snowflake (Terraform)

Snowflake support targets `snowflakedb/snowflake` v2.x (production-grade, Snowflake-owned; the provider was renamed from `Snowflake-Labs/snowflake` in 2025). Pinned to `~> 2.0`. Covers 10 resources: warehouse, database, schema, role, user, grant_account_role, grant_privileges_to_account_role, resource_monitor, network_policy, scim_integration.

Snowflake forces key-pair authentication as of November 2025; password auth is rejected at apply time. Output modes `Snowflake only` and `Okta + Snowflake` use the six required env vars: `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PRIVATE_KEY`, `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` (optional), `SNOWFLAKE_ROLE`, `SNOWFLAKE_WAREHOUSE`.

Composite mode `Okta + Snowflake` wires SCIM provisioning: an `okta_app_oauth` on the Okta side points at the Snowflake SCIM endpoint, and a `snowflake_scim_integration` of type `OKTA` on the Snowflake side accepts the inbound requests. A manual step is required after apply: retrieve the SCIM bearer token from Snowflake (`SELECT SYSTEM$GENERATE_SCIM_ACCESS_TOKEN('OKTA_SCIM');`) and paste it into the Okta SCIM "API token" field. The generated HCL emits a `# NOTE` comment explaining this.

Live Snowflake context lit up in Phase 19c. The env-context fetcher uses `snowflake-connector-python` with key-pair JWT auth (the only auth mode Snowflake still accepts after the November 2025 password deprecation) to run `SHOW WAREHOUSES`, `SHOW DATABASES`, `SHOW ROLES`, and `SHOW USERS` on first session load. The four resource categories are surfaced to the parser so generated HCL references real account names instead of inventing them, and the Snowflake pill flips to `on` when the six secrets `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PRIVATE_KEY`, `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` (optional), `SNOWFLAKE_ROLE`, and `SNOWFLAKE_WAREHOUSE` are configured and the connector handshake succeeds. The fetcher gracefully downgrades a single missing privilege (e.g. a read-only service role that cannot `SHOW USERS`) to a `partial_errors` entry instead of aborting the whole context fetch.

Phase 19c also re-grounded SECTION K of the system prompt against the cached v2.16.0 provider binary at `_tftool/.terraform-plugin-cache/registry.terraform.io/snowflakedb/snowflake/2.16.0/`. The v1 -> v2 rename of `snowflake_role` to `snowflake_account_role` is now reflected in every example, the removed `warehouses` attribute on `snowflake_resource_monitor` no longer appears (warehouse-to-monitor binding flows through the warehouse resource's `resource_monitor` field), `snowflake_scim_integration.enabled` is emitted as required, and `sync_password` is emitted as a string. Users with old generated HCL that still says `snowflake_role` will see a `terraform validate` failure on re-apply; the fix is a global `snowflake_role -> snowflake_account_role` rename in their existing files.

## Feature surface

**Generation pipeline.** Anthropic Haiku 4.5 with prompt caching (system prompts wrapped in `cache_control: ephemeral`); intent parser, generator, and validator/refiner each run as a discrete pass. Live env context from Okta / AWS / GCP resolves real group, app, function, and project IDs into the parsed intent before generation.

**UI.** Dark IBM Plex Mono theme, three-pass progress chip, side-by-side intent vs output expander, validation grouping (errors / warnings / info), output versioning with diff viewer, cancel-mid-generation flag, examples library, account modal, help drawer, in-app feedback widget, keyboard shortcuts (`Cmd+Enter` parse, `Cmd+Shift+G` generate, `Cmd+Shift+P` push), 5-step guided tour persisted in `user_prefs`. Sidebar is collapsible and grouped (Connections / Examples / Activity / Admin / Settings).

**Enterprise readiness (Phase 8 Thread A).** Google OAuth gate, role-based access control (admin / editor / contributor / viewer), per-user daily cost cap, append-only audit log, PII redaction before any prompt leaves Streamlit, 30-minute idle session timeout, secret rotation reminders. See `SECURITY.md` for the full posture.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure secrets

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

Edit `.streamlit/secrets.toml`:

| Key | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Anthropic API key (Haiku 4.5 with prompt caching) |
| `GITHUB_TOKEN` | yes | GitHub PAT with `repo` write scope |
| `GITHUB_REPO` | yes | Target repository in `owner/repo` format |
| `OKTA_API_TOKEN` / `OKTA_ORG_NAME` | optional | Live Okta context for the parser; resolves real group / app IDs instead of placeholders |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | optional | Live AWS context for Lambda function lookups |
| `GCP_SA_JSON` | optional | Single-line JSON service account key for live GCP context (Cloud Functions / Run / Pub/Sub listings); ADC works locally without this |
| `JAMF_INSTANCE_FQDN` / `JAMF_CLIENT_ID` / `JAMF_CLIENT_SECRET` | optional | Live JAMF Pro context (oauth2). Resolves real policy / smart group / script IDs |
| `[auth]`, `[roles]`, `[quotas]` | required for prod | See `SECURITY.md` for the OAuth + RBAC + per-user cost cap configuration |

The GitHub repo must have at least one commit before the first push.

### 3. Run locally

```bash
streamlit run app.py
```

## Usage

1. Type a plain-English description.
2. Select an output mode and any resource-type checkboxes you want to constrain.
3. Review the parsed intent card and confirm.
4. Inspect the generated HCL and code in the side-by-side panels; toggle the "Intent vs output" expander to spot drift.
5. Push to GitHub or download as ZIP.
6. Use Regenerate with extra instructions to refine; the diff viewer shows what changed against the previous version.

## Example prompts

- `Create a SAML app called HR Portal for Workday with SCIM provisioning`
- `Build a Lambda that fires when a user is added to the Offboarding group and sends an SNS alert`
- `Set up a Cloud Function that responds to HTTP requests and returns a JSON status`
- `Create a Pub/Sub topic called orders that fans out to two Cloud Functions`
- `Custom authorization server for our payments API with read:invoices and write:invoices scopes`
- `Group rule that adds users with department=Engineering to the Engineering group`

The Examples library in the sidebar ships pre-seeded prompts across every supported resource for one-click loading.

## Other ways to use this

The Streamlit UI is the primary surface but the same generator runs behind four other entry points. All of them call `core.service.generate()` directly, so a fix to `generator/prompts.py` propagates to every interface without a fork.

### CLI

`cli.py` wraps the generator for shell pipelines and CI jobs. No Streamlit, no Google OAuth, just `ANTHROPIC_API_KEY` and an optional `GITHUB_TOKEN` for `--push`.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python cli.py "Create a SAML app for Salesforce"
python cli.py --stdin --output-dir ./out < prompt.txt
python cli.py "..." --output-mode "Both" --no-refine
python cli.py "..." --push owner/repo --branch feature/auto-tfgen
python cli.py "..." --json   # emit JSON to stdout, no files written
```

Exit codes: `0` success, `1` config error, `2` generation error, `3` push failed.

### HTTP API

A FastAPI app at `api/index.py` exposes three endpoints under `/api/*`. Designed to deploy to Vercel Python serverless with Fluid Compute (lifts the 10-second wall clock so the 3-pass refine completes synchronously). `vercel.json` ships with the right runtime, memory, and 800-second `maxDuration`.

```
GET  /api/health      no auth
POST /api/generate    X-API-Key required
POST /api/push        X-API-Key required
```

Required env vars on the deploy: `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, `GITHUB_REPO`, `TFGEN_API_KEY` (the shared secret callers send in `X-API-Key`). Optional: `ANTHROPIC_MODEL`, `TFGEN_HTTP_DAILY_QUOTA_USD` (default $5/day across all callers; per-key quotas are deferred until traffic justifies a `[api_keys]` table).

Example:

```bash
curl -H "X-API-Key: $TFGEN_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"prompt": "Create an Engineering group", "output_mode": "Both"}' \
     https://your-deploy.vercel.app/api/generate
```

Response: `{"intent": {...}, "files": {"terraform/okta.tf": "...", ...}, "validation_result": {...}, "cost_usd": 0.07, "cost_remaining_usd": 4.93}`.

### Slack `/tfgen`

`api/slack.py` registers a slash-command endpoint on the same FastAPI app. Slack signs every request with `X-Slack-Signature` over body + timestamp; the handler verifies and rejects replays older than 5 minutes.

Flow: user types `/tfgen create an Engineering group` in any Slack channel; handler returns an immediate ephemeral "Working on it" within Slack's 3-second deadline; a FastAPI BackgroundTasks worker runs the generation, pushes to `SLACK_DEFAULT_REPO` on branch `tfgen-slack`, and posts the result (commit URL + a fenced `okta.tf` preview) back to the channel via the slash-command's `response_url`.

Required env vars: `SLACK_SIGNING_SECRET`, `SLACK_DEFAULT_REPO`. The Slack-app install spec lives in `integrations/slack-app-manifest.yaml` so any future workspace install reuses the same scopes (`commands`, `chat:write`) and slash-command shape.

### JIRA webhook

`api/jira.py` accepts JIRA Cloud webhook payloads at `POST /api/jira/webhook`. Verifies an HMAC-SHA256 signature in `X-Hub-Signature` against `JIRA_WEBHOOK_SECRET` (set via JIRA Automation rules or a front proxy), filters to issues that carry the `tfgen` label, and treats `summary + description` as the prompt. Description can be plaintext or Atlassian Document Format JSON; an ADF walker extracts the text.

After generation, pushes to `JIRA_DEFAULT_REPO` on branch `jira/<issue_key>` and posts a JIRA comment via REST v3 with the commit URL and a fenced `okta.tf` preview. HTTP Basic auth on the callback uses `JIRA_USER_EMAIL:JIRA_API_TOKEN` per JIRA Cloud's auth model. Optional auto-transition (e.g. to "In Review") gated behind `JIRA_AUTO_TRANSITION=1` and `JIRA_TRANSITION_ID`; off by default since transition IDs are project-specific.

JIRA's webhook delivery is fire-and-forget with a 30-second window for a 2xx response; the handler completes the full generate within that window thanks to Vercel Fluid Compute, no background-task gymnastics.

### Auth model summary

| Surface | Auth |
|---|---|
| Streamlit app | Google OAuth + RBAC via `roles.toml` |
| CLI | `ANTHROPIC_API_KEY` env var (no service auth) |
| HTTP API | `X-API-Key` header (single shared `TFGEN_API_KEY`) |
| Slack | Slack signing secret + workspace verification |
| JIRA | HMAC-SHA256 webhook signature + Basic-auth callback |

`audit.py` and `cost.py` accept any actor identifier; CLI uses the email if set, HTTP uses `sha256(api_key)[:16]`, Slack uses `sha256(slack_user_id)[:16]`, JIRA uses `sha256(creator_email)[:16]`. Same on-disk JSONL/JSON files, different hash inputs, one audit trail.

## Provider versions

The generated HCL pins:

- `okta/okta ~> 4.0` (currently resolves to 4.20.0; verified against the live provider schema)
- `hashicorp/google ~> 6.0`
- `hashicorp/aws ~> 5.0`

To upgrade Okta to v6 (a breaking change for several resources), bump the constraint in the generated `okta.tf` and run `terraform init -upgrade`. `okta_factor` and several event-hook attributes have schema differences between v4 and v6; expect to re-validate.

## Deploying to Streamlit Community Cloud

1. Push the repo to GitHub.
2. At [share.streamlit.io](https://share.streamlit.io), connect the repo.
3. Paste the contents of `.streamlit/secrets.toml` (including `[auth]`, `[roles]`, `[quotas]`) into the app's Secrets field.
4. Deploy. The pinned `streamlit==1.56.0` in `requirements.txt` avoids a 1.57.0 OAuth-state regression; lift only after confirming an upstream fix.

Streamlit Community Cloud is the live runtime. Local `streamlit run app.py` works for development; ADC handles GCP auth in that mode.

## Lambda deployment note

Generated `lambda.tf` references `../lambda/lambda_function.zip`. Build this before `terraform apply`:

```bash
cd lambda
zip lambda_function.zip lambda_function.py
# if requirements.txt is non-empty:
pip install -r requirements.txt -t package/
cd package && zip -r ../lambda_function.zip . && cd ..
```

## Cloud Function deployment note

Generated `gcp.tf` references `../cloud_function/cloud_function.zip`. Build it the same way:

```bash
cd cloud_function
zip cloud_function.zip main.py requirements.txt
```

A real Cloud Functions Gen2 deployment also requires billing linked on the target project, the `run.googleapis.com`, `cloudfunctions.googleapis.com`, `cloudbuild.googleapis.com`, and `artifactregistry.googleapis.com` APIs enabled, and standard build / compute service-account roles. See `_tftool/validate/run_validate.py` for an automated harness that surfaces these prerequisites.

## Testing

Two suites cover the generator:

**pytest unit + integration** (CI gate, runs on every push and PR via `.github/workflows/ci.yml`):

```bash
pytest tests/
```

Current: **82 / 88 passing**. The 6 skipped failures live in `tests/test_gcp_client.py` and trace to a pre-existing fake-google-modules harness mismatch; CI excludes that file.

**qa_runner.py live regression suite** (133 cases across every supported resource and output mode):

```bash
# full suite, live LLM + terraform validate against locked providers
python qa_runner.py

# replay from cache (free; no live calls)
python qa_runner.py --replay

# skip the terraform validate pass (faster, costs less)
python qa_runner.py --no-terraform-validate

# subset by id
python qa_runner.py PM01 PM02 GCP04
```

Current scores:

- Static QA (intent + must_contain assertions): **130 / 133**
- Terraform validate against locked provider schemas (Both mode): **123 / 133**

`--terraform-validate` is on by default as of `44654b9`; opt out with `--no-terraform-validate` when iterating on prompt-only changes. The three remaining static failures (EH04, EHX03, EDX02) are reproducible event-type sampling issues deferred to a Phase 10 few-shot tuning pass. The remaining ~10 validate failures are tracked in the project memory.

## Architecture

- `app.py`: Streamlit UI; intent parsing, generation, validation, push.
- `generator/`: LLM prompts (`prompts.py`), generation pipeline (`terraform_gen.py`), parser (`parser.py`), refiner (`validator.py`), and deterministic post-generation sanitizers / HCL utilities (`hcl_utils.py`, including `dedupe_variable_blocks` and provider-block merging).
- `okta_client.py`, `aws_client.py`, `gcp_client.py`: live provider context for the parser (resolves real resource IDs).
- `env_context.py`: fan-out across providers; partial-success per service.
- `audit.py`, `cost.py`, `roles.py`, `redact.py`, `secret_rotation.py`: Phase 8 Thread A enterprise modules (audit log, daily quota, RBAC, PII redaction, rotation tracking).
- `ui/`: CSS theme (`css.py`), components (`components.py`), account modal (`account.py`), feedback widget (`feedback.py`), error helpers.
- `qa_runner.py` + `tf_validate.py`: live regression suite + terraform-validate harness.
- `tests/`: pytest suite.
- `_tftool/`: gitignored scratch space for terraform-validate workspaces and dev tools.
- `.github/workflows/ci.yml`: pytest gate on push and PR.
- `.claude/`: Claude Code scoped settings, including a Stop hook that blocks ending a turn with unpushed commits.

## Dependency management

The source of truth for Python deps is `requirements.in`, which holds loose `>=` constraints for the 16 direct dependencies. `requirements.txt` is a fully pinned lockfile generated by `pip-compile` covering every direct and transitive package. To bump a dependency, edit `requirements.in` then run `pip-compile requirements.in -o requirements.txt --strip-extras` and commit both files together. The `lock-check` CI job regenerates the lockfile on every PR and fails if the result drifts from what's checked in, so a `requirements.in` edit that forgets the recompile cannot merge. Dependabot watches `requirements.in` with `versioning-strategy: increase-if-necessary` to keep PR noise low.

## Security

See `SECURITY.md` for authentication, RBAC, audit, cost-cap, PII redaction, session timeout, secret rotation, and what's explicitly out of scope today.
