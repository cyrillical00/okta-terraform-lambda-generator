# PROVIDER_IAM.md, least-privilege guide

> This file is the canonical guide to minimum-permission credentials for every provider TF Tool integrates with. Use the env-context column when configuring the TF Tool's own provider credentials (Streamlit Cloud Settings -> Secrets UI). Use the apply column when configuring the credentials your `terraform apply` / `fleetctl apply` will run under.

## Why least-privilege

TF Tool integrates with 7 credential surfaces (Okta, AWS, GCP, JAMF Pro, Fleet MDM, Snowflake, GitHub). Each surface has two distinct access patterns:

1. **env-context (read-only)**: the TF Tool itself authenticates to the provider's API to list existing resources (groups, apps, lambdas, computer groups, etc.). The lists feed back into the prompt so the generator does not invent identifiers. These credentials live in Streamlit Cloud secrets and are owned by the tool.
2. **apply (write)**: the generated `*.tf` / `fleet-gitops.yaml` files run against your tenancy under your own credentials. TF Tool never sees these.

The principle is simple: each credential should hold the smallest set of permissions that lets its consumer do its job, and nothing more. A leaked env-context token should be incapable of mutating production; a leaked apply credential should be scoped to the resource types your prompts actually generate.

Cross-references:
- Rotation cadences and incident playbooks live in [`SECURITY.md`](SECURITY.md).
- Broken-glass procedures IR-1 through IR-10 (per-provider revocation, blast-radius assessment) live in [`INCIDENT_RESPONSE.md`](INCIDENT_RESPONSE.md).

---

## 1. Okta

TF Tool ships with `okta_client.py`, which authenticates via the classic Okta API token (`SSWS` header). A classic API token inherits the permissions of the admin user who minted it; you constrain it via **Admin Console -> Security -> Administrators -> Admin Roles**, creating a dedicated role with only the resource permissions listed below, then assigning that role to a dedicated service-user. The "scope" names in the table below are the canonical scope strings the Okta API uses when a token is restricted via Admin Role; they are the same names the OAuth 2.0 for Okta Management API uses for service-app credentials.

| Permission | env-context (read-only) | apply (write) |
|---|---|---|
| Groups | `okta.groups.read` | `okta.groups.manage` |
| Applications | `okta.apps.read` | `okta.apps.manage` |
| Event Hooks | `okta.eventHooks.read` | `okta.eventHooks.manage` |
| Authorization Servers | not required | `okta.authorizationServers.manage` (only if the prompt requests `okta_auth_server`) |
| Users | `okta.users.read` (optional; current env-context does not list users by default) | `okta.users.manage` |

### env-context steps (read-only token)

1. Sign in to Okta Admin Console as a Super Admin.
2. Navigate to **Security -> API -> Tokens**.
3. Create a new token from a dedicated read-only service user (e.g. `tf-tool-readonly@yourcompany.com`). The service user's Admin Role should be a custom role with read-only access to Groups, Applications, and Event Hooks (Admin Console -> Security -> Administrators -> Admin Roles -> Create new role).
4. Copy the token value (one-time display) into Streamlit Cloud -> Settings -> Secrets as `OKTA_API_TOKEN`.
5. Also set `OKTA_ORG_URL` to `https://<your-org>.okta.com` (no trailing slash; `okta_client.py` strips it but be tidy).

The exact API surface TF Tool calls (from `okta_client.py`):

```
GET https://<org>.okta.com/api/v1/groups?limit=200
GET https://<org>.okta.com/api/v1/apps?limit=200
GET https://<org>.okta.com/api/v1/eventHooks
```

### apply steps (write token)

1. Mint a separate API token (do not reuse the read-only one) from a service user with write Admin Role on the resource families your prompts emit.
2. Configure the okta Terraform provider per the provider docs: https://registry.terraform.io/providers/okta/okta/latest/docs.
3. Reference: the resource families TF Tool can emit are catalogued in `generator/prompts.py` SECTION G (Okta Resource Schema Reference, starting around line 1898).

Provider block expected by the generator (apply credential is consumed via env vars by the Terraform provider, not by TF Tool):

```hcl
provider "okta" {
  org_name  = var.okta_org_name
  base_url  = var.okta_base_url
  api_token = var.okta_api_token
}
```

---

## 2. AWS

TF Tool's `aws_client.py` uses boto3 with static keys (`AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`). The clients instantiated are `lambda` and `iam`; the listing surface is small.

### env-context IAM policy (read-only)

Attach this inline policy to the dedicated env-context IAM user. Replace nothing; the policy uses no placeholders.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "TFToolEnvContextLambda",
      "Effect": "Allow",
      "Action": [
        "lambda:ListFunctions",
        "lambda:GetFunction"
      ],
      "Resource": "*"
    },
    {
      "Sid": "TFToolEnvContextIAM",
      "Effect": "Allow",
      "Action": [
        "iam:ListRoles",
        "iam:GetRole"
      ],
      "Resource": "*"
    },
    {
      "Sid": "TFToolEnvContextEvents",
      "Effect": "Allow",
      "Action": [
        "events:ListRules"
      ],
      "Resource": "*"
    },
    {
      "Sid": "TFToolEnvContextSNS",
      "Effect": "Allow",
      "Action": [
        "sns:ListTopics"
      ],
      "Resource": "*"
    }
  ]
}
```

Notes:
- `lambda:GetFunction` and `iam:GetRole` are needed because `aws_client.py` exposes `get_lambda_by_name` and `get_role` helpers (see lines 58 and 78). The standard env-context fetcher only calls the `List*` actions; if you want to be even tighter, drop the `Get*` actions.
- `events:ListRules` and `sns:ListTopics` are present because SECTION C (Lambda Rules, around line 1739 of `generator/prompts.py`) emits EventBridge schedules and SNS topics as common Lambda triggers; the env-context lists them so the generator picks existing names instead of inventing new ones.

### env-context user creation (copy-paste AWS CLI)

```bash
aws iam create-user --user-name tf-tool-env-context
aws iam put-user-policy --user-name tf-tool-env-context \
  --policy-name TFToolEnvContextReadOnly \
  --policy-document file://tf-tool-env-context-policy.json
aws iam create-access-key --user-name tf-tool-env-context
```

The final command prints `AccessKeyId` and `SecretAccessKey`. Per [`SECURITY.md`](SECURITY.md), do not paste these into chat; write them to the Streamlit Cloud Secrets UI directly.

### apply IAM policy (write, starter)

The exact permission set depends on what your prompts generate; the policy below covers the common Lambda + EventBridge + SNS stack from SECTION C of `generator/prompts.py`. Extend per resource type.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "LambdaWrite",
      "Effect": "Allow",
      "Action": [
        "lambda:CreateFunction",
        "lambda:UpdateFunctionCode",
        "lambda:UpdateFunctionConfiguration",
        "lambda:DeleteFunction",
        "lambda:AddPermission",
        "lambda:RemovePermission",
        "lambda:TagResource",
        "lambda:UntagResource"
      ],
      "Resource": "*"
    },
    {
      "Sid": "IAMRoleWrite",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:PassRole",
        "iam:TagRole"
      ],
      "Resource": "*"
    },
    {
      "Sid": "EventBridgeWrite",
      "Effect": "Allow",
      "Action": [
        "events:PutRule",
        "events:DeleteRule",
        "events:PutTargets",
        "events:RemoveTargets"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SNSWrite",
      "Effect": "Allow",
      "Action": [
        "sns:CreateTopic",
        "sns:DeleteTopic",
        "sns:SetTopicAttributes",
        "sns:Subscribe",
        "sns:Unsubscribe"
      ],
      "Resource": "*"
    }
  ]
}
```

For production, scope `Resource` to ARN patterns (e.g. `arn:aws:lambda:us-east-1:123456789012:function:tf-tool-*`) instead of `"*"`. Provider docs: https://registry.terraform.io/providers/hashicorp/aws/latest/docs.

---

## 3. GCP

TF Tool's `gcp_client.py` uses a service-account JSON key (`GCP_SA_JSON`). It instantiates `functions_v2`, `iam_admin_v1`, `pubsub_v1`, and `run_v2` clients (see lines 44 to 59 of `gcp_client.py`).

### env-context service-account roles (read-only)

| Role | Why |
|---|---|
| `roles/cloudfunctions.viewer` | `list_functions` on Cloud Functions Gen2 |
| `roles/run.viewer` | `list_run_services` on Cloud Run (Gen2 functions are backed by Cloud Run) |
| `roles/pubsub.viewer` | `list_pubsub_topics` |
| `roles/iam.serviceAccountUser` | `list_service_accounts` (read-only on the project's SAs; does NOT grant impersonation here since the env-context fetcher does not impersonate) |

### env-context steps (copy-paste gcloud CLI)

```bash
gcloud iam service-accounts create tf-tool-env-context \
  --display-name "TF Tool env-context read-only" \
  --project YOUR_PROJECT_ID

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member "serviceAccount:tf-tool-env-context@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role roles/cloudfunctions.viewer

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member "serviceAccount:tf-tool-env-context@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role roles/run.viewer

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member "serviceAccount:tf-tool-env-context@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role roles/pubsub.viewer

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member "serviceAccount:tf-tool-env-context@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role roles/iam.serviceAccountUser

gcloud iam service-accounts keys create tf-tool-env-context.json \
  --iam-account tf-tool-env-context@YOUR_PROJECT_ID.iam.gserviceaccount.com
```

Replace `YOUR_PROJECT_ID` with your GCP project ID (e.g. `acme-prod-12345`). Paste the contents of `tf-tool-env-context.json` into Streamlit Cloud as the value of `GCP_SA_JSON`.

### env-context role binding (Terraform equivalent, using `for_each`)

```hcl
resource "google_service_account" "tf_tool_env_context" {
  account_id   = "tf-tool-env-context"
  display_name = "TF Tool env-context read-only"
  project      = var.gcp_project_id
}

resource "google_project_iam_member" "tf_tool_env_context_roles" {
  for_each = toset([
    "roles/cloudfunctions.viewer",
    "roles/run.viewer",
    "roles/pubsub.viewer",
    "roles/iam.serviceAccountUser",
  ])
  project = var.gcp_project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.tf_tool_env_context.email}"
}
```

### apply roles (write)

The standard Cloud Functions Gen2 stack from `generator/prompts.py` SECTION C2 (line 566 onwards) emits these resources: `google_service_account`, `google_storage_bucket`, `google_storage_bucket_object`, `google_cloudfunctions2_function`, `google_cloud_run_service_iam_member`, plus optional `google_pubsub_topic` and `google_cloud_scheduler_job`.

Minimum roles for the apply principal:

| Role | Why |
|---|---|
| `roles/cloudfunctions.admin` | Create / update / delete `google_cloudfunctions2_function` |
| `roles/run.admin` | Manage the Cloud Run service that backs Gen2 functions, and the `google_cloud_run_service_iam_member` binding |
| `roles/pubsub.admin` | Create topics for event-triggered functions |
| `roles/storage.admin` | Create the source-bundle bucket and upload `cloud_function.zip` |
| `roles/iam.serviceAccountAdmin` | Create the runtime service account for the function |
| `roles/iam.serviceAccountUser` | `actAs` the runtime service account when deploying the function |
| `roles/cloudscheduler.admin` | Required only when prompts emit `google_cloud_scheduler_job` |

Provider docs: https://registry.terraform.io/providers/hashicorp/google/latest/docs.

---

## 4. JAMF Pro

TF Tool's `jamf_client.py` uses OAuth2 client-credentials against `/api/oauth/token` (see line 45 of `jamf_client.py`). The env-context fetcher hits these endpoints:

```
GET /api/v1/policies
GET /api/v2/computer-groups
GET /api/v1/scripts
GET /api/v1/packages
GET /api/v1/computer-extension-attributes
```

### env-context API role (read-only)

In JAMF Pro -> **Settings -> System -> API Roles and Clients**, create a new API Role with these privileges set to **Read**:

| Privilege | Read | Update | Create | Delete |
|---|---|---|---|---|
| Policies | yes | no | no | no |
| Smart Computer Groups | yes | no | no | no |
| Static Computer Groups | yes | no | no | no |
| Scripts | yes | no | no | no |
| Packages | yes | no | no | no |
| Computer Extension Attributes | yes | no | no | no |

Then create a new API Client bound to that role. Copy `Client ID` to `JAMF_CLIENT_ID` and `Client Secret` to `JAMF_CLIENT_SECRET` in Streamlit Cloud Secrets. Also set `JAMF_URL` to your instance URL (e.g. `https://yourcompany.jamfcloud.com`).

Step-by-step:

1. Log in to JAMF Pro as an admin, then **Settings (gear icon) -> System -> API Roles and Clients**.
2. **API Roles** tab -> **+ New**. Display name `TF Tool env-context read-only`. Add the privileges from the table above (every other privilege left unchecked). Save.
3. **API Clients** tab -> **+ New**. Display name `tf-tool-env-context`. API roles: select `TF Tool env-context read-only`. Access token lifetime: leave the default 30 minutes. Enable and save.
4. Click **Generate client secret** and copy the value immediately (one-time display).

### apply API role (write)

The generator emits resources documented in `generator/prompts.py` SECTION D (line 897 onwards): `jamfpro_policy`, `jamfpro_smart_computer_group`, `jamfpro_static_computer_group`, `jamfpro_script`, `jamfpro_package`, `jamfpro_computer_extension_attribute`, `jamfpro_category`, `jamfpro_site`, plus a handful of less-common types.

Create a second API Role (do not reuse the read-only one) with **Read, Update, Create, Delete** set for each of:

- Policies
- Smart Computer Groups
- Static Computer Groups
- Scripts
- Packages
- Computer Extension Attributes
- Categories
- Sites

Bind a second API Client to that role. The apply runbook header on every `terraform_jamf_hcl` (see `generator/prompts.py` line 912) reminds you to run `terraform apply -parallelism=1`; without `-parallelism=1` you will get non-deterministic results regardless of how broad the API role is.

Provider docs: https://registry.terraform.io/providers/deploymenttheory/jamfpro/latest/docs (pinned to `~> 0.37` per the locked provider matrix).

---

## 5. Fleet MDM

TF Tool's `fleet_client.py` uses a bearer token. The env-context fetcher hits these endpoints (see `fleet_client.py` lines 107 to 120):

```
GET /api/v1/fleet/labels
GET /api/v1/fleet/policies
GET /api/v1/fleet/queries
GET /api/v1/fleet/teams
```

Fleet does not have a granular permissions UI; it has built-in roles. Each role is documented at https://fleetdm.com/docs/using-fleet/permissions.

### env-context built-in role (read-only)

| Role | Use for |
|---|---|
| **Observer** | Strictest read-only. Can read hosts, policies, queries, teams. Cannot run live queries that change host state. Recommended for env-context. |
| **Observer+** | Same as Observer, plus the ability to run pre-defined queries. Use only if you intentionally want env-context to execute queries. |

Steps to mint the token:

1. As a Fleet admin, **Settings -> Users -> Create user**. Email `tf-tool-env-context@yourcompany.com`; global role **Observer**. Save.
2. Log in as that user. Avatar (top right) -> **My account** -> **Get API token**.
3. Copy the token to Streamlit Cloud as `FLEET_API_TOKEN`. Set `FLEET_URL` to the Fleet server URL (e.g. `https://fleet.yourcompany.com`).

### apply built-in role (write)

For the GitOps path (`fleetctl apply -f fleet-gitops.yaml`):

- **GitOps Admin**: required for `fleetctl apply`. This role specifically wraps the GitOps workflow and allows declarative configuration of teams, policies, queries, and config profiles.

For the experimental Terraform path (`l-teles/fleetdm` provider, see `generator/prompts.py` SECTION J around line 2456):

- **Maintainer**: covers create / update / delete on labels, policies, queries, and team-scoped resources. Not Admin; Admin grants user-management which the Terraform provider does not need.

Mint a separate token from a dedicated GitOps Admin (or Maintainer) user. Do not give your env-context Observer user write privileges; rotation cadence and blast-radius scale with role scope (per [`SECURITY.md`](SECURITY.md) `FLEET_API_TOKEN` row).

Provider docs:
- GitOps: https://fleetdm.com/docs/configuration/yaml-files
- Terraform: https://registry.terraform.io/providers/l-teles/fleetdm/latest/docs

---

## 6. Snowflake

Phase 19c lit up the Snowflake env-context fetcher (`snowflake_client.py` + `env_context.fetch_snowflake_context`). The `SNOWFLAKE_*` env vars now drive both the apply-time provider authentication AND the read-only `SHOW WAREHOUSES`, `SHOW DATABASES`, `SHOW ROLES`, and `SHOW USERS` calls the env-context layer issues on session load. The two columns below describe the live recipe.

### env-context role (read-only)

The env-context fetcher uses key-pair JWT auth to connect, then runs the four `SHOW` statements above. The `MONITOR USAGE ON ACCOUNT` grant is what lets a non-admin role see warehouses and roles account-wide; without it the role can only see objects it owns.

```sql
USE ROLE SECURITYADMIN;

CREATE ROLE TF_TOOL_READER COMMENT = 'TF Tool env-context read-only role';

GRANT USAGE ON WAREHOUSE COMPUTE_WH TO ROLE TF_TOOL_READER;
GRANT USAGE ON DATABASE ANALYTICS TO ROLE TF_TOOL_READER;
GRANT USAGE ON ALL SCHEMAS IN DATABASE ANALYTICS TO ROLE TF_TOOL_READER;
GRANT USAGE ON FUTURE SCHEMAS IN DATABASE ANALYTICS TO ROLE TF_TOOL_READER;

-- Account-level SHOW statements need MONITOR USAGE; without it the fetcher
-- still returns connected=True but every SHOW returns an empty list.
GRANT MONITOR USAGE ON ACCOUNT TO ROLE TF_TOOL_READER;

-- SHOW USERS requires a higher privilege; on a read-only service role this
-- typically returns "Insufficient privileges to operate on USERS" and the
-- fetcher downgrades the single error to a partial_errors entry. Grant
-- MANAGE GRANTS only if your env-context needs the user list.
-- GRANT MANAGE GRANTS ON ACCOUNT TO ROLE TF_TOOL_READER;

GRANT ROLE TF_TOOL_READER TO USER TF_TOOL_SERVICE;
```

Replace `COMPUTE_WH`, `ANALYTICS`, and `TF_TOOL_SERVICE` with your actual warehouse, database, and service-user names.

Service-user setup (key-pair auth, since Snowflake deprecated password auth Nov 2025):

```sql
USE ROLE USERADMIN;
CREATE USER TF_TOOL_SERVICE
  DEFAULT_ROLE = TF_TOOL_READER
  DEFAULT_WAREHOUSE = COMPUTE_WH
  RSA_PUBLIC_KEY = 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCg...';
```

The `RSA_PUBLIC_KEY` value is the PEM-encoded public key body (strip the `-----BEGIN PUBLIC KEY-----` / `-----END PUBLIC KEY-----` lines and join on one line). Generate the key-pair locally with the command documented in [`SECURITY.md`](SECURITY.md) under the `SNOWFLAKE_PRIVATE_KEY` rotation block.

Set Streamlit Cloud secrets:
- `SNOWFLAKE_ACCOUNT` = `xy12345.us-east-1`
- `SNOWFLAKE_USER` = `TF_TOOL_SERVICE`
- `SNOWFLAKE_PRIVATE_KEY` = (full PEM, including BEGIN/END lines)
- `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` = (optional, only if encrypted)
- `SNOWFLAKE_ROLE` = `TF_TOOL_READER`
- `SNOWFLAKE_WAREHOUSE` = `COMPUTE_WH`

### apply role (write)

The generator emits resources documented in `generator/prompts.py` SECTION K (line 2712 onwards). The minimum apply role depends on what your prompts touch:

| Apply scope | Role |
|---|---|
| Non-security resources only: `snowflake_warehouse`, `snowflake_database`, `snowflake_schema`, `snowflake_table`, `snowflake_view`, `snowflake_function`, `snowflake_procedure` | `SYSADMIN` |
| Anything in the SCIM / authentication / network-policy / OAuth space: `snowflake_scim_integration`, `snowflake_oauth_integration`, `snowflake_saml_integration`, `snowflake_network_policy`, `snowflake_user`, `snowflake_account_role`, `snowflake_grant_*` | `SECURITYADMIN` |
| Composite Okta + Snowflake mode (SCIM provisioning, see SECTION K composite-mode subsection) | `SECURITYADMIN` (the `snowflake_scim_integration` resource of type `OKTA` requires it) |

For applies that span both classes, use `SECURITYADMIN` and accept the wider blast radius, or split the apply into two Terraform states each scoped to its minimum role.

Set the apply role explicitly in the provider's environment, not in HCL:

```bash
export SNOWFLAKE_ROLE=SECURITYADMIN
terraform apply
```

Provider docs: https://registry.terraform.io/providers/snowflakedb/snowflake/latest/docs (pinned to `~> 2.0`).

---

## 8. Kandji

Phase 23 lit up the Kandji env-context fetcher (`kandji_client.py` + `env_context.fetch_kandji_context`). The `KANDJI_*` env vars drive both apply-time provider authentication AND the read-only `GET /api/v1/blueprints`, `/api/v1/library/library-items`, and `/api/v1/tags` calls the env-context layer issues on session load. Kandji rebranded to Iru in late 2025 and the Terraform provider source moved to `MScottBlake/iru`, but the REST API hosts kept the legacy `kandji.io` domain.

### env-context token (read-only)

The env-context fetcher uses a tenant-scoped bearer token to list blueprints, library items, and tags. Tokens are minted from Settings -> Access -> Add API Token in the Kandji web console. Each token has a permission matrix; the read-only roles needed by the env-context fetcher are:

| Permission | Why |
|---|---|
| Blueprints: Read | populates the blueprints list in the prompt context |
| Library Items: Read | populates the library items list |
| Tags: Read | populates the tags list |

Set Streamlit Cloud secrets:
- `KANDJI_BASE_URL` = `https://<your-subdomain>.api.kandji.io` (US region) or `https://<your-subdomain>.clients.eu.kandji.io` (EU region)
- `KANDJI_API_TOKEN` = (tenant bearer token; treat as sensitive)

The API caps at 10,000 requests per hour per customer. The fetcher walks pagination at 300 items per page with a hard safety cap of 100 pages (30,000 items) per endpoint. A 429 response surfaces the `Retry-After` header in the partial-errors trail.

### apply token (write)

The generator emits resources documented in `generator/prompts.py` SECTION L. Write scopes required for `terraform apply` depend on what your prompts touch:

| Apply scope | Required Kandji token permissions |
|---|---|
| Blueprints + routing: `iru_blueprint`, `iru_blueprint_routing`, `iru_blueprint_library_item` | Blueprints: Read/Write |
| Library items: `iru_custom_script`, `iru_custom_profile`, `iru_custom_app`, `iru_in_house_app` | Library Items: Read/Write |
| Device-level: `iru_tag`, `iru_device_note` | Devices: Read/Write |
| Apple Business Manager integration: `iru_ade_integration`, `iru_ade_device` | ADE: Read/Write + Blueprints: Read/Write |

For applies that span all four classes, mint a single apply token with the four Read/Write permissions and accept the wider blast radius, or split the apply into multiple Terraform states each scoped to its minimum permission set.

Use a separate token for env-context (read-only) and apply (write); rotating one without disturbing the other is the whole reason to split them.

Provider docs: https://registry.terraform.io/providers/MScottBlake/iru/latest/docs (pinned to `~> 0.0`; current published version 0.0.10).

---

## 9. Lumos

Phase 24 lit up the Lumos env-context fetcher (`lumos_client.py` + `env_context.fetch_lumos_context`). Lumos is the identity-governance / access-management plane that overlays IdPs like Okta with a request catalog, pre-approval rules, and access-policy bundles. The `LUMOS_ACCESS_TOKEN` env var drives both apply-time provider authentication AND the read-only `GET /apps`, `/groups`, and `/requestable_permissions` calls the env-context layer issues on session load. The official Terraform provider is `teamlumos/lumos` (OpenAPI-generated by Speakeasy; current published version 0.10.3).

### env-context token (read-only)

The env-context fetcher uses a Personal Access Token (PAT) prefixed `lsk_`. PATs are minted from Settings -> Developers -> Personal Access Tokens in the Lumos web console. Lumos PATs are scope-bearing; the read-only scopes needed by the env-context fetcher are:

| Permission | Why |
|---|---|
| Apps: Read | populates the apps list in the prompt context |
| Groups: Read | populates the groups list (used to cross-reference Okta-side groups in lumos_pre_approval_rule.preapproved_groups) |
| Requestable Permissions: Read | populates the requestable-permissions list |

Set Streamlit Cloud secrets:
- `LUMOS_ACCESS_TOKEN` = (PAT, prefix `lsk_`; treat as sensitive)

Base URL is fixed at `https://api.lumos.com`; tenants are identified by the bearer token itself, so there is no per-tenant URL secret. EU and on-premise deployments override the `server_url` provider attribute on the apply side only.

Pagination uses an opaque `next_page_token` cursor; the client walks tokens with a safety cap of 100 pages per endpoint. Rate limits are not publicly documented; 429 responses surface the `Retry-After` header in the partial-errors trail.

### apply token (write)

The generator emits resources documented in `generator/prompts.py` SECTION M. Write scopes required for `terraform apply` depend on what your prompts touch:

| Apply scope | Required Lumos token permissions |
|---|---|
| Custom apps: `lumos_app` | Apps: Read/Write |
| App-store installs: `lumos_app_store_app` | App Store: Read/Write |
| Access policies: `lumos_access_policy` | Access Policies: Read/Write + Apps: Read |
| Pre-approval rules: `lumos_pre_approval_rule` | Pre-Approval Rules: Read/Write + Apps: Read + Groups: Read |
| Requestable permissions: `lumos_requestable_permission` | Apps: Read/Write |

For applies that span pre-approval and access-policy resources, mint a single apply token with the union of those scopes. Lumos's permission model is coarser than Okta's; least-privilege at the Lumos level means scoping PATs by *family* of resource, not per-app.

Use a separate token for env-context (read-only) and apply (write); rotating one without disturbing the other is the whole reason to split them. Lumos PATs do not currently expire automatically; rotate every 90 days to match the global cadence in [`SECURITY.md`](SECURITY.md).

Provider docs: https://registry.terraform.io/providers/teamlumos/lumos/latest/docs (pinned to `~> 0.10`; current published version 0.10.3).

---

## 7. GitHub

GitHub is **push-target only** for TF Tool. There is no env-context fetcher; the tool does not list repos, branches, or PRs before generating. The `GITHUB_TOKEN` secret is used exclusively by `gh_push/` to commit the generated files to a repo and (optionally) open a PR.

| Surface | env-context | apply / push |
|---|---|---|
| GitHub | not used | `repo` scope (private repos) or `public_repo` scope (public repos only) |

### Token type and scopes

Two options:

| Token type | When to use | Required scopes |
|---|---|---|
| Fine-grained personal access token (recommended) | When you push to a specific repo or org you control | Repository access: select the target repo(s). Repository permissions: **Contents: Read and write**, **Pull requests: Read and write**, **Metadata: Read-only** (automatic). |
| Classic personal access token | When you push to many repos across orgs and don't want to re-scope on every onboarding | **`repo`** for private repos; **`public_repo`** is enough if all targets are public. |

Create at: https://github.com/settings/personal-access-tokens (fine-grained) or https://github.com/settings/tokens (classic).

Steps for fine-grained:

1. Log in to GitHub as the bot identity (do not use a human's personal account; `gh_push/` uses this same identity for every TF Tool caller, per `SECURITY.md` line 174).
2. https://github.com/settings/personal-access-tokens -> **Generate new token**. Token name `tf-tool-push`; expiration 90 days (matches `SECURITY.md` rotation cadence).
3. Repository access: **Only select repositories** -> add the target repo(s).
4. Permissions: Contents **Read and write**, Pull requests **Read and write**, Metadata **Read-only** (auto-set).
5. **Generate token**, copy the value, paste into Streamlit Cloud Secrets as `GITHUB_TOKEN`.

### Note: GITHUB_TOKEN is separate from the Streamlit `[auth]` Google OAuth credentials

The Streamlit `[auth]` block in `secrets.toml` holds the Google OAuth client ID and client secret that authenticate **end users** logging in to the TF Tool UI. That is a completely different credential surface; do not conflate it with `GITHUB_TOKEN`. The Google OAuth credential lets a user prove who they are; the `GITHUB_TOKEN` is the bot's push identity to GitHub. See [`SECURITY.md`](SECURITY.md) for the full credential matrix.

---

## Cross-references

- **Rotation cadences and revocation procedures**: [`SECURITY.md`](SECURITY.md). Every secret listed in this guide has a documented rotation interval (typically 90 to 180 days) and a per-secret revocation walkthrough.
- **Incident response (IR-1 through IR-10)**: [`INCIDENT_RESPONSE.md`](INCIDENT_RESPONSE.md). Per-provider broken-glass procedures:
  - IR-2: GitHub PAT leak
  - IR-3: JAMF Pro credentials leak
  - IR-4: Fleet API token leak
  - IR-5: Snowflake key-pair compromise
  - IR-6: Okta API token leak
  - IR-7: AWS access key leak
  - IR-8: GCP service account key leak
- **Resource schema references** (for working out what apply-side permissions you actually need):
  - Okta: `generator/prompts.py` SECTION G, line 1898
  - GCP: `generator/prompts.py` SECTION C2, line 566
  - JAMF Pro: `generator/prompts.py` SECTION D, line 897
  - Fleet MDM (GitOps): `generator/prompts.py` SECTION I, line 2209
  - Fleet MDM (Terraform): `generator/prompts.py` SECTION J, line 2456
  - Snowflake: `generator/prompts.py` SECTION K, line 2712
  - Kandji (Iru): `generator/prompts.py` SECTION L
  - Lumos: `generator/prompts.py` SECTION M

## Update cadence

Review this file whenever:
- A provider adds a new resource type to TF Tool (new `prompts.py` section or new resource family in an existing section). The apply-side role/policy may need extending.
- A provider deprecates an authentication method (e.g. Snowflake password auth in Nov 2025, which forced the `SNOWFLAKE_PRIVATE_KEY` migration).
- A new env-context fetcher is added (e.g. a future `snowflake_client.py`). Promote the "forward-looking" section to authoritative.
- An incident response surfaces an over-privileged credential. Tighten the minimum and document the change here before closing the IR ticket.
