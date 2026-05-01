# QA Test Suite

`qa_runner.py` ships a 132-case live-LLM regression suite that exercises every supported resource type, output mode, and known failure mode. It runs against the live Anthropic API (with prompt caching), checks generated output for forbidden patterns, schema compliance, and `must_contain` presence, and reports PASS/FAIL with cost telemetry.

## Running it

```bash
# full suite (live, ~$1.50, ~7 minutes)
python qa_runner.py

# filter by test ID
python qa_runner.py GR01 GR02 EH04 GCPX01

# replay from cache (no API spend; cache must be populated first)
python qa_runner.py --replay
```

Each run writes `qa_outputs_cache.json` (per-test outputs), `qa_report.json` (per-test status + issues), and stdout shows progress + final summary including cache-hit rate and estimated cost.

## What gets validated

`run_checks` (in `qa_runner.py`) applies 15 sections of static checks against each generated output:

1. **Output-mode contracts**: Okta-only mode must not leak AWS resources; Lambda-only must not leak Okta; etc.
2. **Hallucinated removal attributes** on `okta_group_rule` (provider has no `remove_assigned_group_ids`).
3. **`okta_event_hook` schema**: required `events = [...]` set + `channel` map; forbidden `events_filter`, `filters`, `auth_type`.
4. **Group-membership scenarios**: when prompt mentions "added to / removed from / joins", the event type must be `group.user_membership.add` or `.remove`.
5. **`must_contain` substrings**: per-test expected strings present in `terraform_okta_hcl`.
6. **`must_not_contain_okta` substrings**: per-test forbidden strings absent.
7. **Both-mode AWS sanity**: when AWS resources requested, Lambda code must not be empty.
8. **Lambda handler signature**: `def handler(event, context):` present.
9. **Hardcoded-secret patterns**: sk-ant-, AKIA…, raw api_token strings.
10. **Expected resource type**: parser classified the prompt into the right Okta resource family.
11. **`okta_group_rule` schema**: `expression_value`, `group_assignments`; 50-char name limit.
12. **`okta_app_saml` SCIM**: `provisioning {}` blocks forbidden (sanitizer strips them; this is the belt-and-suspenders check).
13. **`okta_brand` v4 attrs**: no `logo`, `primary_color`, `secondary_color` (sanitized).
14. **`okta_network_zone`**: `gateways` for IP zones, `dynamic_locations` / `asns` for DYNAMIC, never both.
15. **GCP module**: provider boilerplate, Gen2 only (no `google_cloudfunctions_function`), authoritative IAM forbidden, GCP-only mode contract, `must_contain_gcp` substrings.

## Test categories (132 total)

### Okta core resources (76)

| Category | Count | What it covers |
|---|---|---|
| `G` | 5 | `okta_group` basics: simple group creation with name + description |
| `GR` | 5 | `okta_group_rule`: department, country, title-based attribute matching |
| `GRX` | 4 | Group rule extras: VP / FTE / EMEA / premium-tier rules |
| `EH` | 10 | `okta_event_hook` event-type selection across the full Okta event taxonomy |
| `EHX` | 5 | Event hook extras: profile updates, password changes, complex group scenarios |
| `AS` | 6 | `okta_app_saml`: Salesforce, Workday, ServiceNow, Box, HR Portal (with SCIM NOTE) |
| `SA` | 3 | SAML advanced: attribute statements, multi-group assignment, profile mapping |
| `AO` | 3 | `okta_app_oauth` basic: web, SPA, machine-to-machine |
| `OA` | 3 | OAuth advanced: PKCE, web with code grant, service account |
| `OAX` | 3 | OAuth extras: mobile PKCE, web auth code, service-account credentials |
| `AUTH` | 5 | `okta_auth_server`: payments, mobile, custom claims |
| `AP` | 2 | `okta_auth_server_policy` and policy rule with token lifetime |
| `SC` | 3 | `okta_auth_server_scope`: read, write, default openid |
| `CL` | 3 | `okta_auth_server_claim`: groups, role, department |
| `MFA` | 5 | `okta_factor`: Google Authenticator, Okta Verify, Duo, FIDO2, YubiKey |
| `NZ` | 2 | `okta_network_zone` IP allow / block |
| `NZD` | 3 | Dynamic network zones: country-based, ASN-based, geo-based |
| `BR` | 1 | `okta_brand` (sanitized to drop `logo`, `primary_color`, `secondary_color`) |
| `EM` | 2 | `okta_email_customization` activation and forgot-password templates |
| `EMX` | 4 | Email extras: password-changed, email-challenge, AD-forgot-password, account-locked |
| `PM` | 5 | `okta_user_profile_mapping`: Workday → Okta, Salesforce → Okta, custom attrs |

### Output-mode contracts (14)

| Category | Count | What it covers |
|---|---|---|
| `OO` | 5 | Okta-only mode: zero AWS / GCP leak in the generated output |
| `OOX` | 4 | Okta-only extras: auth server, network zone, profile mapping, email |
| `OPT` | 5 | `optional_tf` collision: must not redefine `aws_lambda_function`, `aws_iam_role`, etc. |

### AWS Lambda + Okta integration (8)

| Category | Count | What it covers |
|---|---|---|
| `AW` | 4 | Event hook + Lambda: deactivation, scheduled inactive sweep, Lambda URL, SNS notification |
| `AWX` | 4 | Lambda extras: API Gateway, scheduled review, group-add SNS alert, weekly deprovision |

### GCP module (8)

| Category | Count | What it covers |
|---|---|---|
| `GCP` | 5 | Cloud Function HTTP, Pub/Sub trigger, Cloud Run service, Scheduler+Function, Okta+GCP composite |
| `GCPX` | 3 | Complex GCP: Pub/Sub fan-out, GCS object-finalize trigger, Secret Manager + IAM-bound Function |

### Edge cases and compound workflows (20)

| Category | Count | What it covers |
|---|---|---|
| `ED` | 5 | Edge: terminated-group cleanup, role transitions, department-based rules, mutual exclusivity |
| `EDX` | 4 | Edge extras: archive-only group rule, tier transitions, beta tester rule, Greenhouse SAML |
| `COMP` | 11 | Compound multi-resource workflows including the 3 most complex tests in the suite |

The 11 `COMP` tests are the hardest in the suite. Notable ones:

- `COMP01-08`: original compound tests: OAuth + auth server + scope, SAML + multi-group, payments auth + scopes + claims, OIDC + group restriction, Terminated group + event hook, full onboarding email sequence, etc.
- `COMP09`: full onboarding workflow: 3 groups + 3 group rules + Workday SAML + 3 group assignments + event hook on `user.lifecycle.create` + Lambda.
- `COMP10`: zero-trust API access: custom auth server + 3 scopes + 2 claims + access policy + policy rule + 2 OAuth apps (mobile + web). Currently hits parser `max_tokens=1024` ceiling; surfaces a real generator-side limit.
- `COMP11`: offboarding pipeline: Terminated group + group rule + event hook on `group.user_membership.add` + Lambda + SNS + Okta Verify factor.

## Real `terraform validate`

`qa_runner.py` validates by string presence; it does NOT run `terraform validate` against the locked provider. A separate dev tool does:

```bash
# populate cache first
python qa_runner.py

# then validate every cached output against okta/okta + hashicorp/google + hashicorp/aws
python _tftool/validate/run_validate.py

# or filter
python _tftool/validate/run_validate.py EH01 GCPX01 COMP09
```

The harness writes one workspace per test under `_tftool/validate/<TID>/`, runs `terraform init -backend=false` (provider download is shared via `TF_PLUGIN_CACHE_DIR`), then `terraform validate`. PASS means the HCL parses against the actual provider schema; static QA cannot catch provider-schema drift on its own.

`_tftool/` is gitignored. The script is a dev tool, not committed.

## Cost

A full live run is ~$1.50 with prompt caching (99% cache hit on the cached prefix; Haiku 4.5 pricing $1 / $5 / $1.25 / $0.10 per M for input / output / cache write / cache read). A targeted N-test re-run is roughly `N * $0.012`. The `--replay` mode is free (no API calls).

Pre-cache-control runs of the same suite cost ~$15. Caching is enforced in `parser.py`, `terraform_gen.py`, and `validator.py` via `cache_control: {"type": "ephemeral"}` on every system prompt.

## Adding a test

```python
TestCase("MYTEST01",
         "Plain-English prompt the user might type.",
         okta_types=["okta_group"],          # parser hint; sets output_mode if any types provided
         aws_types=["aws_lambda_function"],  # if Both mode
         gcp_types=[...],                     # if GCP mode
         expected_resource_type="okta_group", # parser must classify this
         must_contain=["okta_group", "Engineering"],   # substrings required in terraform_okta_hcl
         must_not_contain_okta=HALLUCINATED_REMOVE_ATTRS,
         must_contain_gcp=["..."],            # required in terraform_gcp_hcl
         notes="Free-form note explaining edge case"),
```

Mode mapping (`build_intent` in `qa_runner.py`):

| `okta_types` | `aws_types` | `gcp_types` | Resulting `output_mode` |
|---|---|---|---|
| set | unset | unset | `Okta Terraform only` |
| any | set | unset | `Both` |
| unset | set | unset | `Lambda only` |
| any | any | set | `Okta + GCP` if okta also set, else `GCP only` |

If `must_contain` references a substring that lives in `terraform_okta_hcl`, you must set `okta_types` so the runner produces non-empty Okta output. Otherwise the substring check fails against an empty string.
