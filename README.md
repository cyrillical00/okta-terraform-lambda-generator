# Okta Terraform + Lambda Generator

A Streamlit app that converts plain-English Okta operation descriptions into production-ready **Terraform HCL** and **AWS Lambda Python**, then pushes both to GitHub with one click.

## What it generates

- `terraform/okta.tf` — Okta provider config + Okta resource (app, group, event hook, etc.)
- `terraform/lambda.tf` — AWS IAM role + Lambda function resource
- `lambda/lambda_function.py` — Python 3.11 Lambda handler (event hook verification, scheduled, or API Gateway)
- `lambda/requirements.txt` — Lambda pip dependencies (if any)

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure secrets

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

Edit `.streamlit/secrets.toml` and fill in:

| Key | Description |
|-----|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `GITHUB_TOKEN` | GitHub personal access token with `repo` write scope |
| `GITHUB_REPO` | Target repository in `owner/repo` format |

> The GitHub repository must have at least one existing commit before pushing.

### 3. Run

```bash
streamlit run app.py
```

## Usage

1. Enter a plain-English description of the Okta operation
2. Review and edit the parsed intent card
3. Click **Confirm and Generate**
4. Review the Terraform HCL and Lambda Python in the side-by-side panels
5. Click **Push to GitHub** to commit all four files, or **Download as ZIP** to save locally
6. Use **Regenerate** with optional extra instructions to refine the output

## Example prompts

- `Create an Okta SAML app called HR Portal for Workday`
- `Build a Lambda that fires when a user is deactivated in Okta and logs the event`
- `Set up an RBAC group called Engineering with a rule that matches users in the engineering department`
- `Create an Okta event hook that triggers on user.lifecycle.deactivate`
- `Scheduled Lambda that runs nightly and checks for Okta users without MFA enrolled`

## Deploying to Streamlit Community Cloud

1. Push this repo to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io) and connect the repo
3. In the app settings, paste the contents of your `secrets.toml` into the **Secrets** field
4. Deploy

## Okta Terraform provider version

Generated HCL targets `hashicorp/okta ~> 4.0`. The current stable provider release is `6.x`. To upgrade, change the version constraint in the generated `okta.tf` and run `terraform init -upgrade`.

## Lambda deployment note

The generated `lambda.tf` references `../lambda/lambda_function.zip`. You need to build this ZIP before running `terraform apply`:

```bash
cd lambda
zip lambda_function.zip lambda_function.py
# If requirements.txt is non-empty:
pip install -r requirements.txt -t package/
cd package && zip -r ../lambda_function.zip . && cd ..
```
