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

Composite modes (Okta+AWS, Okta+GCP) automatically merge `terraform { required_providers {} }` blocks so the generated files coexist in a single Terraform module without "Duplicate required providers configuration" errors.

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
| `ANTHROPIC_API_KEY` | yes | Anthropic API key (Haiku 4.5 used for generation; prompt caching enabled) |
| `GITHUB_TOKEN` | yes | GitHub PAT with `repo` write scope |
| `GITHUB_REPO` | yes | Target repository in `owner/repo` format |
| `OKTA_API_TOKEN` / `OKTA_ORG_NAME` | optional | Live Okta context for the parser; resolves real group / app IDs instead of placeholders |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | optional | Live AWS context for Lambda function lookups |
| `GCP_SA_JSON` | optional | Single-line JSON service account key for live GCP context (Cloud Functions / Run / Pub/Sub listings); ADC works locally without this |

The GitHub repo must have at least one commit before the first push.

### 3. Run

```bash
streamlit run app.py
```

## Usage

1. Type a plain-English description.
2. Select an output mode and any resource-type checkboxes you want to constrain.
3. Review the parsed intent card and confirm.
4. Inspect the generated HCL and code in the side-by-side panels.
5. Push to GitHub or download as ZIP.
6. Use Regenerate with extra instructions to refine.

## Example prompts

- `Create a SAML app called HR Portal for Workday with SCIM provisioning`
- `Build a Lambda that fires when a user is added to the Offboarding group and sends an SNS alert`
- `Set up a Cloud Function that responds to HTTP requests and returns a JSON status`
- `Create a Pub/Sub topic called orders that fans out to two Cloud Functions`
- `Custom authorization server for our payments API with read:invoices and write:invoices scopes`
- `Group rule that adds users with department=Engineering to the Engineering group`

## Provider versions

The generated HCL pins:

- `okta/okta ~> 4.0` (currently resolves to 4.20.0; verified against the live provider schema)
- `hashicorp/google ~> 6.0`
- `hashicorp/aws ~> 5.0`

To upgrade Okta to v6 (a breaking change for several resources), bump the constraint in the generated `okta.tf` and run `terraform init -upgrade`. Note: `okta_factor` and several event hook attributes have schema differences between v4 and v6; expect to re-validate.

## Deploying to Streamlit Community Cloud

1. Push the repo to GitHub.
2. At [share.streamlit.io](https://share.streamlit.io), connect the repo.
3. Paste the contents of `.streamlit/secrets.toml` into the app's Secrets field.
4. Deploy. The pinned `streamlit==1.56.0` in `requirements.txt` avoids a 1.57.0 OAuth-state regression; lift only after confirming an upstream fix.

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

## QA suite

`qa_runner.py` ships a 132-case live regression suite covering every supported resource and output mode, plus a separate `terraform validate` harness under `_tftool/validate/`. See `TESTS.md` for the full breakdown and how to run individual cases.

```bash
# full suite (~$1.50 live, ~7 min)
python qa_runner.py

# replay from cache (free)
python qa_runner.py --replay

# real terraform validate against locked providers
python _tftool/validate/run_validate.py
```

## Architecture

- `app.py`: Streamlit UI; intent parsing, generation, validation, push.
- `generator/`: LLM prompts (`prompts.py`), generation pipeline (`terraform_gen.py`), parser (`parser.py`), refiner (`validator.py`), and deterministic post-generation sanitizers for `okta_brand`, `okta_app_saml` SCIM, `okta_group`, and provider-block merging (`hcl_utils.py`).
- `okta_client.py`, `aws_client.py`, `gcp_client.py`: live provider context for the parser (resolves real resource IDs).
- `env_context.py`: fan-out across providers; partial-success per service.
- `qa_runner.py`: live LLM regression suite.
- `tests/`: unit tests for sanitizers and HCL utilities (`pytest tests/`).
- `_tftool/`: gitignored scratch space for terraform-validate workspaces and dev tools.
