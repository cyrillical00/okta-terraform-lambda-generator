import os

import anthropic
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="Okta TF+Lambda Generator",
    page_icon="🔐",
    layout="wide",
)

from generator.parser import parse_intent, validate_intent
from generator.terraform_gen import generate_all, GenerationError
from generator.lambda_gen import validate_lambda_python
from generator.validator import validate_outputs, fix_outputs, refine_outputs
from generator.okta_group_sanitizer import sanitize_okta_group_refs
from generator.hcl_utils import strip_provider_boilerplate, derive_basename_from_intent
from gh_push.push import push_to_github, build_commit_message
from ui.components import render_intent_card, render_code_panels, render_action_buttons, render_validation_result, render_optional_tf, render_tfvars_example, render_resource_type_selector
import history as _history
from history import add_entry, get_entries
from env_context import build_env_context, format_context_for_prompt
from repo_context import fetch_terraform_files, format_repo_context_for_prompt

_OKTA_RESOURCE_TYPES = {
    "okta_app_saml", "okta_app_oauth", "okta_group", "okta_group_rule",
    "okta_event_hook", "okta_user_profile_mapping", "okta_auth_server",
    "okta_auth_server_scope", "okta_auth_server_claim",
    "okta_auth_server_policy", "okta_auth_server_policy_rule",
    "okta_factor", "okta_network_zone", "okta_brand", "okta_email_customization",
}


def _get_secret(key: str) -> str:
    val = st.secrets.get(key) or os.getenv(key, "")
    return val.strip() if val else ""


def _init_session_state():
    defaults = {
        "intent": None,
        "outputs": None,
        "output_mode": "Both",
        "parse_error": None,
        "gen_error": None,
        "commit_url": None,
        "generation_triggered": False,
        "validation_result": None,
        "last_user_input": "",
        "env_context": None,
        "repo_tf_files": None,
        "repo_tf_error": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _get_client() -> anthropic.Anthropic:
    api_key = _get_secret("ANTHROPIC_API_KEY")
    if not api_key:
        st.error("ANTHROPIC_API_KEY is not configured. Add it to .streamlit/secrets.toml or set it as an environment variable.")
        st.stop()
    if not api_key.startswith("sk-ant"):
        st.error(f"ANTHROPIC_API_KEY looks wrong — it should start with 'sk-ant' but starts with '{api_key[:8]}...'. Check your Streamlit secrets.")
        st.stop()
    return anthropic.Anthropic(api_key=api_key)


def _get_model(default: str) -> str:
    return _get_secret("ANTHROPIC_MODEL") or default


def _build_files(outputs: dict, mode: str, base: str = "") -> dict[str, str]:
    """Build the file map for GitHub push.

    base: optional filename base used to namespace generated files so that
    multiple prompts can coexist in the same target repo without overwriting
    each other (e.g. prompt #1 -> base="engineering" -> terraform/engineering.tf;
    prompt #2 -> base="hr_portal" -> terraform/hr_portal.tf). When empty, the
    legacy fixed paths (terraform/okta.tf, terraform/lambda.tf, etc.) are used
    so single-prompt usage is unchanged.

    Empty-content outputs are skipped so an Okta-only generation does not push
    a zero-byte terraform/lambda.tf placeholder.
    """
    files = {}
    okta_hcl = outputs.get("terraform_okta_hcl", "")
    lambda_hcl = outputs.get("terraform_lambda_hcl", "")
    lambda_py = outputs.get("lambda_python", "")
    lambda_reqs = outputs.get("lambda_requirements", "")
    optional_tf = outputs.get("optional_tf", "")
    tfvars = outputs.get("terraform_tfvars_example", "")

    if base:
        okta_path = f"terraform/{base}.tf"
        lambda_tf_path = f"terraform/{base}_lambda.tf"
        lambda_py_path = f"lambda/{base}.py"
        lambda_reqs_path = f"lambda/{base}_requirements.txt"
        optional_path = f"terraform/{base}_optional_extensions.tf"
        tfvars_path = f"terraform/{base}.tfvars.example"
    else:
        okta_path = "terraform/okta.tf"
        lambda_tf_path = "terraform/lambda.tf"
        lambda_py_path = "lambda/lambda_function.py"
        lambda_reqs_path = "lambda/requirements.txt"
        optional_path = "terraform/optional_extensions.tf"
        tfvars_path = "terraform/terraform.tfvars.example"

    # When base is set, the file lives alongside an existing terraform/okta.tf
    # (or providers.tf) that already declares the boilerplate. Stripping
    # avoids "Duplicate ..." init errors. When base is empty, this is the
    # canonical single-file push and the boilerplate must remain.
    if base:
        if okta_hcl:
            okta_hcl = strip_provider_boilerplate(okta_hcl)
        if lambda_hcl:
            lambda_hcl = strip_provider_boilerplate(lambda_hcl)

    if mode in ("Both", "Okta Terraform only"):
        if okta_hcl and okta_hcl.strip():
            files[okta_path] = okta_hcl
        if lambda_hcl and lambda_hcl.strip():
            files[lambda_tf_path] = lambda_hcl
    if mode in ("Both", "Lambda only"):
        if lambda_py and lambda_py.strip():
            files[lambda_py_path] = lambda_py
        if lambda_reqs and lambda_reqs.strip():
            files[lambda_reqs_path] = lambda_reqs
    if optional_tf and optional_tf.strip():
        files[optional_path] = optional_tf
    if tfvars and tfvars.strip():
        files[tfvars_path] = tfvars
    return files


def _generate_and_refine(intent: dict, extra_instructions: str, client, model: str) -> dict:
    """Generate outputs then run up to 3 validate→fix passes. Uses st.status for progress."""
    outputs = None
    error = None
    should_rerun = False
    env_section = format_context_for_prompt(st.session_state.env_context or {})
    repo_section = format_repo_context_for_prompt(st.session_state.repo_tf_files or {})
    provider_version = intent.get("provider_version", "~> 4.0")

    with st.status("Generating...", expanded=True) as status:
        try:
            st.write("Generating initial output...")
            outputs = generate_all(intent, extra_instructions, client, model=model, env_context_section=env_section, provider_version=provider_version, repo_context_section=repo_section)

            syntax_errors = validate_lambda_python(outputs["lambda_python"])
            if syntax_errors:
                st.write(f"⚠️ Lambda syntax warning: {'; '.join(syntax_errors)}")

            def _on_pass(pass_num, result, has_issues):
                if has_issues:
                    n = len(result.get("terraform_issues", [])) + len(result.get("lambda_issues", []))
                    st.write(f"Pass {pass_num}/3: fixing {n} issue(s)...")
                else:
                    st.write(f"Pass {pass_num}/3: looks good.")

            outputs = refine_outputs(
                intent=intent,
                outputs=outputs,
                user_input=st.session_state.last_user_input,
                client=client,
                model=model,
                on_pass=_on_pass,
                output_mode=intent.get("output_mode", "Both"),
            )

            # Deterministic post-refinement cleanup: rewrite hallucinated
            # `data "okta_group"` blocks (name not in live org) into
            # `resource "okta_group"` blocks. No-op when Okta is not connected.
            live_groups = (st.session_state.env_context or {}).get("okta", {}).get("groups") or []
            outputs = sanitize_okta_group_refs(outputs, live_groups)

            status.update(label="Done", state="complete", expanded=False)
        except GenerationError as e:
            error = e
            status.update(label="Generation failed", state="error", expanded=False)

    if error:
        st.session_state.gen_error = str(error)
        with st.expander("Raw response from Claude"):
            st.code(error.raw_response)
        return None

    return outputs


def _load_env_context() -> None:
    """Fetch Okta/AWS context once per session. Skips if already loaded."""
    if st.session_state.env_context is not None:
        return
    st.session_state.env_context = build_env_context(
        okta_org_url=_get_secret("OKTA_ORG_URL"),
        okta_api_token=_get_secret("OKTA_API_TOKEN"),
        aws_region=_get_secret("AWS_REGION"),
        aws_access_key=_get_secret("AWS_ACCESS_KEY_ID"),
        aws_secret_key=_get_secret("AWS_SECRET_ACCESS_KEY"),
    )


def _render_env_sidebar() -> None:
    ctx = st.session_state.env_context or {}
    okta = ctx.get("okta", {})
    aws = ctx.get("aws", {})

    st.sidebar.divider()
    st.sidebar.markdown("**Environment**")

    if okta.get("connected"):
        n_groups = len(okta.get("groups", []))
        n_apps = len(okta.get("apps", []))
        n_hooks = len(okta.get("event_hooks", []))
        st.sidebar.success(f"Okta: {n_groups} groups · {n_apps} apps · {n_hooks} hooks")
    else:
        err = okta.get("error", "Not configured")
        st.sidebar.caption(f"Okta: {err}")

    if aws.get("connected"):
        n_fns = len(aws.get("lambda_functions", []))
        n_roles = len(aws.get("iam_roles", []))
        st.sidebar.success(f"AWS: {n_fns} functions · {n_roles} roles")
    else:
        err = aws.get("error", "Not configured")
        st.sidebar.caption(f"AWS: {err}")

    if st.sidebar.button("Refresh environment", use_container_width=True):
        st.session_state.env_context = None
        st.rerun()


def _render_repo_sidebar(default_repo: str) -> None:
    github_token = _get_secret("GITHUB_TOKEN")
    st.sidebar.divider()
    st.sidebar.markdown("**Connected Terraform Repo**")

    if not github_token:
        st.sidebar.caption("Add GITHUB_TOKEN to secrets to enable repo import.")
        return

    repo_input = st.sidebar.text_input(
        "Repository (owner/repo)",
        value=default_repo,
        placeholder="owner/repo-name",
        key="repo_tf_repo_input",
    )
    path_input = st.sidebar.text_input(
        "Terraform path",
        value="terraform",
        placeholder="terraform",
        key="repo_tf_path_input",
        help="Directory inside the repo containing .tf files (e.g. 'terraform', 'infra', or leave blank for root)",
    )

    col_load, col_clear = st.sidebar.columns(2)
    load_clicked = col_load.button("Load", use_container_width=True, key="repo_tf_load")
    clear_clicked = col_clear.button("Clear", use_container_width=True, key="repo_tf_clear")

    if load_clicked and repo_input.strip():
        try:
            files = fetch_terraform_files(github_token, repo_input.strip(), path_input.strip())
            st.session_state.repo_tf_files = files
            st.session_state.repo_tf_error = None
        except RuntimeError as e:
            st.session_state.repo_tf_files = None
            st.session_state.repo_tf_error = str(e)
        st.rerun()

    if clear_clicked:
        st.session_state.repo_tf_files = None
        st.session_state.repo_tf_error = None
        st.rerun()

    if st.session_state.repo_tf_error:
        st.sidebar.error(st.session_state.repo_tf_error)
    elif st.session_state.repo_tf_files is not None:
        files = st.session_state.repo_tf_files
        if files:
            st.sidebar.success(f"{len(files)} .tf file(s) loaded — generation will use this context")
            for path in files:
                lines = files[path].count("\n") + 1
                st.sidebar.caption(f"· {path} ({lines} lines)")
        else:
            st.sidebar.warning("No .tf files found at that path.")


def _render_history_sidebar(email: str) -> None:
    entries = get_entries(email)
    st.sidebar.divider()
    st.sidebar.markdown("**Command History**")
    if not entries:
        st.sidebar.caption("No history yet. Generate something to start building your library.")
        return

    for i, entry in enumerate(entries[:30]):
        preview = entry["input"][:52] + ("…" if len(entry["input"]) > 52 else "")
        badge = f"`{entry['operation_type']}` · `{entry['resource_type']}`"
        ts = entry.get("timestamp", "")[:10]

        with st.sidebar.container():
            col_text, col_btn = st.sidebar.columns([5, 1])
            with col_text:
                st.caption(f"{badge}  {ts}")
                st.markdown(f"<span style='font-size:0.85em'>{preview}</span>", unsafe_allow_html=True)
            with col_btn:
                if st.button("↺", key=f"reuse_{i}", help=entry["input"]):
                    st.session_state.user_input_area = entry["input"]
                    st.session_state.intent = None
                    st.session_state.outputs = None
                    st.session_state.validation_result = None
                    st.session_state.commit_url = None
                    st.session_state.parse_error = None
                    st.rerun()


_init_session_state()
_history.configure(
    github_token=_get_secret("GITHUB_TOKEN"),
    github_repo=_get_secret("GITHUB_REPO"),
)

# Auth gate
if not hasattr(st.user, "is_logged_in"):
    st.error(
        "Google auth is not configured. "
        "Add `[auth]` and `[auth.google]` sections to your Streamlit secrets and restart the app."
    )
    st.stop()

if not st.user.is_logged_in:
    st.title("Okta Terraform + Lambda Generator")
    st.markdown("Sign in with your Google account to continue.")
    st.button("Sign in with Google", on_click=st.login, args=("google",))
    st.stop()

with st.sidebar:
    st.markdown(f"Signed in as **{st.user.email}**")
    st.button("Sign out", on_click=st.logout)

_load_env_context()
_render_env_sidebar()
_render_repo_sidebar(_get_secret("GITHUB_REPO"))
_render_history_sidebar(st.user.email)

st.title("Okta Terraform + Lambda Generator")
st.caption("Describe an Okta operation in plain English and get production-ready Terraform HCL and AWS Lambda Python.")

# Stage 1 — Input
with st.container():
    okta_types, aws_types = render_resource_type_selector()
    user_input = st.text_area(
        "Describe the Okta operation",
        placeholder='e.g. "Create a SAML app for Google Workspace with SCIM provisioning" or "Build a Lambda that fires when a user is deactivated in Okta"',
        height=100,
        key="user_input_area",
    )
    parse_clicked = st.button("Parse Intent", type="primary")

if parse_clicked and user_input.strip():
    st.session_state.parse_error = None
    st.session_state.intent = None
    st.session_state.outputs = None
    st.session_state.commit_url = None
    st.session_state.validation_result = None
    st.session_state.last_user_input = user_input.strip()
    client = _get_client()
    model = _get_model("claude-haiku-4-5-20251001")
    with st.spinner("Parsing intent..."):
        try:
            intent = parse_intent(user_input.strip(), client, model=model, resource_type_hints=okta_types)
            # Friendly rejection: parser returned 'unknown' and the user gave no UI hints to override.
            if intent.get("resource_type") == "unknown" and not okta_types:
                notes = intent.get("notes") or []
                reason = notes[0] if notes else "The prompt does not appear to describe an Okta infrastructure operation."
                st.session_state.parse_error = (
                    f"This does not look like an Okta operation. {reason} "
                    "Try describing a specific Okta resource: a group, app, event hook, "
                    "auth server, MFA factor, network zone, brand, or email template."
                )
            else:
                if okta_types:
                    # Merge: UI types set the primary type(s); parser adds any compound supporting types
                    parser_extras = [t for t in intent.get("resource_types", []) if t not in set(okta_types)]
                    intent["resource_types"] = list(okta_types) + parser_extras
                elif not intent.get("resource_types"):
                    # Parser didn't return a list — fall back to single type
                    intent["resource_types"] = [intent.get("resource_type", "")]
                if aws_types:
                    intent["aws_resource_types"] = aws_types
                intent["output_mode"] = "Both" if aws_types else "Okta Terraform only"
                errors = validate_intent(intent)
                if errors:
                    st.session_state.parse_error = "Validation errors: " + "; ".join(errors)
                else:
                    st.session_state.intent = intent
        except ValueError as e:
            st.session_state.parse_error = str(e)

if st.session_state.parse_error:
    st.error(st.session_state.parse_error)

# Stage 2 — Clarifying questions
if st.session_state.intent and st.session_state.outputs is None:
    confirmed = render_intent_card(st.session_state.intent)
    if confirmed is not None:
        st.session_state.intent = confirmed
        st.session_state.output_mode = confirmed.get("output_mode", "Both")
        st.session_state.generation_triggered = True

# Stage 3 — Generation with automatic 3-pass refinement
if st.session_state.generation_triggered:
    st.session_state.generation_triggered = False
    st.session_state.gen_error = None
    client = _get_client()
    model = _get_model("claude-haiku-4-5-20251001")
    outputs = _generate_and_refine(st.session_state.intent, "", client, model)
    if outputs is not None:
        st.session_state.outputs = outputs
        add_entry(st.user.email, st.session_state.last_user_input, st.session_state.intent)

if st.session_state.gen_error:
    st.error(st.session_state.gen_error)

# Stage 4 — Display + actions
if st.session_state.outputs:
    mode = st.session_state.output_mode
    render_code_panels(st.session_state.outputs, mode)
    render_optional_tf(st.session_state.outputs.get("optional_tf", ""))
    render_tfvars_example(st.session_state.outputs.get("terraform_tfvars_example", ""))

    col_check, _ = st.columns([1, 3])
    with col_check:
        check_clicked = st.button("Run Self-Check", use_container_width=True)

    if check_clicked:
        client = _get_client()
        model = _get_model("claude-haiku-4-5-20251001")
        with st.spinner("Running independent review..."):
            st.session_state.validation_result = validate_outputs(
                user_input=st.session_state.last_user_input,
                intent=st.session_state.intent,
                outputs=st.session_state.outputs,
                client=client,
                model=model,
                output_mode=st.session_state.output_mode,
            )

    if st.session_state.validation_result:
        fix_clicked = render_validation_result(st.session_state.validation_result)
        if fix_clicked:
            client = _get_client()
            model = _get_model("claude-haiku-4-5-20251001")
            with st.spinner("Fixing issues..."):
                try:
                    vr = st.session_state.validation_result
                    all_issues = vr.get("terraform_issues", []) + vr.get("lambda_issues", [])
                    issues_text = "\n".join(f"- {i}" for i in all_issues)

                    # Detect resource types mentioned in issues that are absent from current output
                    okta_hcl = st.session_state.outputs.get("terraform_okta_hcl", "")
                    current_types = set(st.session_state.intent.get("resource_types", []))
                    missing_types = [
                        rt for rt in _OKTA_RESOURCE_TYPES
                        if rt in issues_text and rt not in okta_hcl
                    ]

                    if missing_types:
                        # Expand resource_types and do a full regeneration
                        expanded_intent = {
                            **st.session_state.intent,
                            "resource_types": list(current_types | set(missing_types)),
                        }
                        fixed = generate_all(
                            intent=expanded_intent,
                            extra_instructions=(
                                f"The previous generation was missing these resources — "
                                f"include them now:\n{issues_text}"
                            ),
                            client=client,
                            model=model,
                        )
                        st.session_state.intent = expanded_intent
                    else:
                        # No missing resources — use targeted fix_outputs
                        optional_tf = st.session_state.outputs.get("optional_tf", "")
                        fixed = fix_outputs(
                            intent=st.session_state.intent,
                            outputs=st.session_state.outputs,
                            validation_result=vr,
                            client=client,
                            model=model,
                        )
                        if optional_tf and not fixed.get("optional_tf"):
                            fixed["optional_tf"] = optional_tf

                    st.session_state.outputs = fixed
                    st.session_state.validation_result = None
                    st.session_state.commit_url = None
                    st.rerun()
                except GenerationError as e:
                    st.error(f"Fix failed: {e}")
                    with st.expander("Raw response from Claude"):
                        st.code(e.raw_response)

    default_repo = _get_secret("GITHUB_REPO")
    auto_basename = derive_basename_from_intent(st.session_state.intent)
    push_clicked, regenerate_clicked, extra_instructions, repo_override, branch_override, file_basename = render_action_buttons(
        st.session_state.outputs, mode, default_repo, auto_basename=auto_basename
    )

    # Regenerate with automatic 3-pass refinement
    if regenerate_clicked:
        st.session_state.gen_error = None
        client = _get_client()
        model = _get_model("claude-haiku-4-5-20251001")
        outputs = _generate_and_refine(st.session_state.intent, extra_instructions, client, model)
        if outputs is not None:
            st.session_state.outputs = outputs
            st.session_state.commit_url = None
            st.session_state.validation_result = None
            st.rerun()

    # GitHub push
    if push_clicked:
        github_token = _get_secret("GITHUB_TOKEN")
        if not github_token:
            st.error("GITHUB_TOKEN must be configured in secrets to push to GitHub.")
        elif not repo_override:
            st.error("Repository name is required to push to GitHub.")
        else:
            files = _build_files(st.session_state.outputs, mode, base=file_basename)
            commit_message = build_commit_message(st.session_state.intent)
            with st.spinner("Pushing to GitHub..."):
                try:
                    commit_url = push_to_github(
                        files, repo_override, github_token, commit_message, branch=branch_override
                    )
                    st.session_state.commit_url = commit_url
                except RuntimeError as e:
                    st.error(str(e))
                except Exception as e:
                    st.error(f"GitHub push failed: {e}")

# Stage 5 — Commit URL
if st.session_state.commit_url:
    st.success("Successfully pushed to GitHub!")
    st.link_button("View commit", st.session_state.commit_url)
