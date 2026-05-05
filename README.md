# Okta Terraform + Lambda + GCP Generator

A Streamlit app that turns plain-English infrastructure descriptions into deployable Terraform HCL across three providers. Output covers Okta resources, AWS Lambda glue (Okta event hooks calling Lambdas, scheduled sweeps), and GCP Cloud Functions / Cloud Run / Pub/Sub. One click pushes the generated files to GitHub; another saves a ZIP locally.

Live at https://okta-terraform-lambda-generator.streamlit.app.

## What it generates

Five output modes selectable from the sidebar:

| Mode | Files |
|---|---|
| Okta Terraform only | `terraform/okta.tf` |
| Both | `terraform/okta.tf`, `terraform/lambda.tf`, `lambda/lambda_function.py`, `lambda/requirements.txt` |
| Lambda only | `terraform/lambda.tf`, `lambda/lambda_function.py`, `lambda/requirements.txt` |
| GCP only | `terraform/gcp.tf`, `cloud_function/main.py`, `cloud_function/requirements.txt` |
| Okta + GCP | `terraform/okta.tf`, `terraform/gcp.tf`, `cloud_function/main.py`, `cloud_function/requirements.txt` |

Composite modes (Okta+AWS, Okta+GCP) automatically merge `terraform { required_providers {} }` blocks and dedupe `variable "X" {}` declarations so the generated files coexist in a single Terraform module without duplicate-block errors.

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

## Security

See `SECURITY.md` for authentication, RBAC, audit, cost-cap, PII redaction, session timeout, secret rotation, and what's explicitly out of scope today.
