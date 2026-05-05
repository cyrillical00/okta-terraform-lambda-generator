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
from generator.validator import validate_outputs, fix_outputs
from generator.hcl_utils import strip_provider_boilerplate, derive_basename_from_intent
from core import service as core_service
from gh_push.push import push_to_github, build_commit_message
from ui.components import (
    render_intent_card, render_code_panels, render_action_buttons,
    render_validation_result, render_optional_tf, render_tfvars_example,
    render_resource_type_selector, render_hero_starters, render_mode_chip,
    render_env_pills, render_gcp_partial_warning, render_success_card,
    render_version_switcher, render_diff_viewer,
    render_intent_output_compare, render_feedback_widget,
    render_error_panel,
)
from ui.examples import render_examples_library
from ui.css import inject_global_css, inject_keyboard_shortcuts, inject_theme
import history as _history
from history import add_entry, get_entries
import audit as _audit
import cost as _cost
import roles as _roles
import redact as _redact
import secret_rotation as _rotation
import user_prefs as _user_prefs
import feedback as _feedback
from ui.onboarding import render as render_onboarding_tour
from ui.account import render_sidebar_links as render_account_links, render_dialogs as render_account_dialogs
from env_context import build_env_context
from repo_context import fetch_terraform_files

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
        # Phase 8B D1.1: persistent push target across reruns. Seeded from
        # the configured GITHUB_REPO secret on first load; user edits in the
        # sidebar or push panel are written back here so the value survives
        # parse/generate/regenerate cycles.
        "b_persisted_repo": _get_secret("GITHUB_REPO"),
        # Phase 8B D3: output versioning. Newest at index 0; max 3 entries.
        # Each entry: {"outputs": dict, "intent": dict, "ts": iso8601 str}.
        "b_output_history": [],
        "b_active_version": 0,
        # Phase 8B B.2: cancel-mid-generation flag. Best-effort — Streamlit
        # is single-threaded per session, so the flag is honored between
        # refinement passes (in _on_pass) but cannot interrupt a single
        # blocking LLM call. The sidebar button that sets this flag is
        # therefore only clickable between script reruns; in practice that
        # means it cancels the NEXT pass after the in-flight one finishes.
        "cancel_requested": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


class _GenerationCancelled(Exception):
    """Raised inside the refinement loop's per-pass callback when the user
    has set st.session_state.cancel_requested. Caught by _generate_and_refine
    so cancellation surfaces as a clean info message + audit entry rather
    than an unhandled traceback."""
    pass


def _push_output_version(outputs: dict, intent: dict | None) -> None:
    """Phase 8B D3: prepend the freshly generated outputs to b_output_history,
    cap the list at 3 entries, and reset the active-version pointer to 0
    (newest). Called from every site where new outputs land — initial
    generation, fix-issues, and regenerate."""
    from datetime import datetime, timezone
    history = list(st.session_state.get("b_output_history") or [])
    history.insert(0, {
        "outputs": outputs,
        "intent": intent,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    st.session_state["b_output_history"] = history[:3]
    st.session_state["b_active_version"] = 0


def _get_client() -> anthropic.Anthropic:
    api_key = _get_secret("ANTHROPIC_API_KEY")
    if not api_key:
        st.error("ANTHROPIC_API_KEY is not configured. Add it to .streamlit/secrets.toml or set it as an environment variable.")
        st.stop()
    if not api_key.startswith("sk-ant"):
        st.error(f"ANTHROPIC_API_KEY looks wrong — it should start with 'sk-ant' but starts with '{api_key[:8]}...'. Check your Streamlit secrets.")
        st.stop()
    raw = anthropic.Anthropic(api_key=api_key)
    # Wrap the client so every messages.create() records usage against the
    # signed-in user. Has no effect if st.user isn't available yet (e.g. at
    # auth-gate render time, before login).
    email = getattr(getattr(st, "user", None), "email", "") or "anonymous"
    return _cost.wrap_client(raw, email)


def _quota_block_or_warn() -> bool:
    """Check the signed-in user's daily quota. Returns True if blocked
    (caller should refuse the action). Renders an inline error when
    blocked. Quota of 0 means unlimited (admins by default)."""
    email = getattr(getattr(st, "user", None), "email", "") or ""
    if not email:
        return False
    spent = _cost.today_usd(email)
    if _roles.is_quota_exhausted(email, spent):
        cap = _roles.daily_quota_usd(email)
        st.error(
            f"Daily spend limit reached: ${spent:.2f} of ${cap:.2f} used today (UTC). "
            "Resets at midnight UTC. Contact an admin to raise your quota."
        )
        _audit.log(email, "quota_blocked", extra={"role": _roles.get_role(email), "spent_usd": spent, "cap_usd": cap})
        return True
    return False


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
    gcp_hcl = outputs.get("terraform_gcp_hcl", "")
    lambda_py = outputs.get("lambda_python", "")
    lambda_reqs = outputs.get("lambda_requirements", "")
    cloud_function_py = outputs.get("cloud_function_python", "")
    cloud_function_reqs = outputs.get("cloud_function_requirements", "")
    optional_tf = outputs.get("optional_tf", "")
    tfvars = outputs.get("terraform_tfvars_example", "")

    if base:
        okta_path = f"terraform/{base}.tf"
        lambda_tf_path = f"terraform/{base}_lambda.tf"
        gcp_tf_path = f"terraform/{base}_gcp.tf"
        lambda_py_path = f"lambda/{base}.py"
        lambda_reqs_path = f"lambda/{base}_requirements.txt"
        cloud_function_py_path = f"cloud_function/{base}.py"
        cloud_function_reqs_path = f"cloud_function/{base}_requirements.txt"
        optional_path = f"terraform/{base}_optional_extensions.tf"
        tfvars_path = f"terraform/{base}.tfvars.example"
    else:
        okta_path = "terraform/okta.tf"
        lambda_tf_path = "terraform/lambda.tf"
        gcp_tf_path = "terraform/gcp.tf"
        lambda_py_path = "lambda/lambda_function.py"
        lambda_reqs_path = "lambda/requirements.txt"
        cloud_function_py_path = "cloud_function/main.py"
        cloud_function_reqs_path = "cloud_function/requirements.txt"
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
        if gcp_hcl:
            gcp_hcl = strip_provider_boilerplate(gcp_hcl)

    if mode in ("Both", "Okta Terraform only", "Okta + GCP"):
        if okta_hcl and okta_hcl.strip():
            files[okta_path] = okta_hcl
    if mode in ("Both",):
        if lambda_hcl and lambda_hcl.strip():
            files[lambda_tf_path] = lambda_hcl
    if mode in ("Both", "Lambda only"):
        if lambda_py and lambda_py.strip():
            files[lambda_py_path] = lambda_py
        if lambda_reqs and lambda_reqs.strip():
            files[lambda_reqs_path] = lambda_reqs
    if mode in ("GCP only", "Okta + GCP"):
        if gcp_hcl and gcp_hcl.strip():
            files[gcp_tf_path] = gcp_hcl
        if cloud_function_py and cloud_function_py.strip():
            files[cloud_function_py_path] = cloud_function_py
        if cloud_function_reqs and cloud_function_reqs.strip():
            files[cloud_function_reqs_path] = cloud_function_reqs
    if optional_tf and optional_tf.strip():
        files[optional_path] = optional_tf
    if tfvars and tfvars.strip():
        files[tfvars_path] = tfvars
    return files


def _generate_and_refine(intent: dict, extra_instructions: str, client, model: str) -> dict:
    """Generate outputs then run up to 3 validate-fix passes. Uses st.status for progress.

    All non-UI orchestration lives in core.service.generate_from_intent.
    This wrapper is the Streamlit UI shell: it owns st.status, the
    progress bar, the inline pass messages, the cancel-flag reset, the
    cancellation audit log, and the GenerationError surface as gen_error
    plus a raw-response expander.
    """
    # Reset the cancel flag at the start of every generation so a stale True
    # from a prior session can't immediately abort this one.
    st.session_state["cancel_requested"] = False

    result = None
    with st.status("Generating...", expanded=True) as status:
        st.write("Pass 0/3: drafting initial output...")
        progress = st.progress(0.0, text="Pass 0/3 · drafting")
        first_pass = {"done": False}

        def _on_pass(pass_num, validation, has_issues):
            # First time we hit a pass callback, the initial draft has
            # finished; surface any lambda syntax warnings before the
            # refinement messages start.
            if not first_pass["done"]:
                first_pass["done"] = True
            progress.progress(pass_num / 3.0, text=f"Pass {pass_num}/3")
            if has_issues:
                n_tf = len(validation.get("terraform_issues", []))
                n_lam = len(validation.get("lambda_issues", []))
                parts = []
                if n_tf:
                    parts.append(f"{n_tf} terraform")
                if n_lam:
                    parts.append(f"{n_lam} lambda")
                detail = " + ".join(parts) if parts else f"{n_tf + n_lam} issue(s)"
                st.write(f"Pass {pass_num}/3 · refining ({detail})")
            else:
                st.write(f"Pass {pass_num}/3 · clean")

        def _cancel_check():
            return bool(st.session_state.get("cancel_requested"))

        result = core_service.generate_from_intent(
            intent,
            client=client,
            model=model,
            user_input=st.session_state.last_user_input,
            output_mode=intent.get("output_mode", "Both"),
            provider_version=intent.get("provider_version", "~> 4.0"),
            env_context=st.session_state.env_context or {},
            repo_context_files=st.session_state.repo_tf_files or {},
            extra_instructions=extra_instructions,
            on_pass=_on_pass,
            cancel_check=_cancel_check,
        )

        if result.cancelled:
            status.update(label="Cancelled", state="error", expanded=False)
        elif result.error:
            status.update(label="Generation failed", state="error", expanded=False)
        else:
            # Surface lambda syntax warnings from the final outputs, matching
            # the prior placement (after the initial draft, before status close).
            syntax_errors = validate_lambda_python(result.outputs.get("lambda_python", ""))
            if syntax_errors:
                st.write(f"Lambda syntax warning: {'; '.join(syntax_errors)}")
            status.update(label="Done", state="complete", expanded=False)

    if result.cancelled:
        st.session_state["cancel_requested"] = False
        st.info("Generation cancelled. The partial output above (if any) has been discarded.")
        try:
            email = getattr(getattr(st, "user", None), "email", "") or ""
            if email:
                _audit.log(email, "gen_cancelled", resource_type=intent.get("resource_type", ""), output_mode=intent.get("output_mode", ""))
        except Exception:
            pass
        return None

    if result.error:
        st.session_state.gen_error = result.error
        with st.expander("Raw response from Claude"):
            st.code(result.error_raw_response)
        return None

    return result.outputs


def _load_env_context() -> None:
    """Fetch Okta/AWS/GCP context once per session. Skips if already loaded.
    Wraps the live-context fetch in st.status so the user sees activity when
    Okta or AWS is slow to respond.
    """
    if st.session_state.env_context is not None:
        return
    with st.status("Connecting to Okta, AWS, GCP...", expanded=False) as status:
        st.session_state.env_context = build_env_context(
            okta_org_url=_get_secret("OKTA_ORG_URL"),
            okta_api_token=_get_secret("OKTA_API_TOKEN"),
            aws_region=_get_secret("AWS_REGION"),
            aws_access_key=_get_secret("AWS_ACCESS_KEY_ID"),
            aws_secret_key=_get_secret("AWS_SECRET_ACCESS_KEY"),
            gcp_project_id=_get_secret("GCP_PROJECT_ID"),
            gcp_sa_json=_get_secret("GCP_SA_JSON"),
            gcp_region=_get_secret("GCP_REGION") or "us-central1",
        )
        ctx = st.session_state.env_context or {}
        connected = sum(
            1 for k in ("okta", "aws", "gcp") if ctx.get(k, {}).get("connected")
        )
        status.update(
            label=f"Live context ready: {connected} of 3 providers connected",
            state="complete",
        )


def _render_env_sidebar() -> None:
    """Renders Okta/AWS/GCP status into the current container. Caller is
    responsible for placing this inside an outer sidebar group; calls use
    bare `st.*` so the writes flow into whatever container the caller
    has open (an expander, in the new sidebar layout)."""
    ctx = st.session_state.env_context or {}
    okta = ctx.get("okta", {})
    aws = ctx.get("aws", {})
    gcp = ctx.get("gcp", {})

    st.markdown("**Environment**")

    if okta.get("connected"):
        n_groups = len(okta.get("groups", []))
        n_apps = len(okta.get("apps", []))
        n_hooks = len(okta.get("event_hooks", []))
        st.success(f"Okta: {n_groups} groups · {n_apps} apps · {n_hooks} hooks")
    else:
        err = okta.get("error", "Not configured")
        st.caption(f"Okta: {err}")

    if aws.get("connected"):
        n_fns = len(aws.get("lambda_functions", []))
        n_roles = len(aws.get("iam_roles", []))
        st.success(f"AWS: {n_fns} functions · {n_roles} roles")
    else:
        err = aws.get("error", "Not configured")
        st.caption(f"AWS: {err}")

    if gcp.get("connected"):
        n_fns = len(gcp.get("functions", []))
        n_sa = len(gcp.get("service_accounts", []))
        n_topics = len(gcp.get("pubsub_topics", []))
        st.success(f"GCP: {n_fns} functions · {n_sa} SAs · {n_topics} topics")
        partial = gcp.get("partial_errors") or []
        if partial:
            # Flattened from an inner expander to inline captions; Streamlit
            # 1.56 forbids nested expanders, and this block now lives inside
            # the outer Connections expander.
            st.caption(f"GCP partial: {len(partial)} service(s) unavailable")
            for p in partial:
                st.caption(f"· {p[:140]}")
    else:
        err = gcp.get("error", "Not configured")
        st.caption(f"GCP: {err}")

    if st.button("Refresh environment", use_container_width=True):
        _audit.log(st.user.email, "env_refresh")
        st.session_state.env_context = None
        st.rerun()


def _render_repo_sidebar(default_repo: str) -> None:
    """Renders the connected-repo input + load/clear into the current container."""
    github_token = _get_secret("GITHUB_TOKEN")
    st.markdown("**Connected Terraform Repo**")

    if not github_token:
        st.caption("Add GITHUB_TOKEN to secrets to enable repo import.")
        return

    repo_input = st.text_input(
        "Repository (owner/repo)",
        value=default_repo,
        placeholder="owner/repo-name",
        key="repo_tf_repo_input",
    )
    # Persist the user's repo choice across reruns and into the push panel.
    if repo_input and repo_input.strip() != (st.session_state.get("b_persisted_repo") or ""):
        st.session_state.b_persisted_repo = repo_input.strip()
    path_input = st.text_input(
        "Terraform path",
        value="terraform",
        placeholder="terraform",
        key="repo_tf_path_input",
        help="Directory inside the repo containing .tf files (e.g. 'terraform', 'infra', or leave blank for root)",
    )

    col_load, col_clear = st.columns(2)
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
        st.error(st.session_state.repo_tf_error)
    elif st.session_state.repo_tf_files is not None:
        files = st.session_state.repo_tf_files
        if files:
            st.success(f"{len(files)} .tf file(s) loaded — generation will use this context")
            for path in files:
                lines = files[path].count("\n") + 1
                st.caption(f"· {path} ({lines} lines)")
        else:
            st.warning("No .tf files found at that path.")


def _render_audit_sidebar(email: str) -> None:
    """Show the user's last 10 privileged actions plus a CSV export button.
    Renders into the current container; caller wraps in an outer expander."""
    st.markdown("**Audit log**")
    entries = _audit.recent(email, limit=10)
    if not entries:
        st.caption("No actions logged yet.")
        return
    for entry in entries:
        ts = (entry.get("timestamp_utc") or "")[:19].replace("T", " ")
        action = entry.get("action", "")
        rt = entry.get("resource_type", "")
        cost = entry.get("cost_estimate_usd", 0.0) or 0.0
        meta = f"`{action}`"
        if rt:
            meta += f" · `{rt}`"
        if cost > 0:
            meta += f" · ${cost:.4f}"
        st.caption(meta)
        st.markdown(f'<span class="tf-sidebar-timestamp">{ts} UTC</span>', unsafe_allow_html=True)
    csv_text = _audit.export_csv(email)
    if csv_text:
        st.download_button(
            "Export full audit (CSV)",
            data=csv_text,
            file_name=f"audit_{_audit._email_hash(email)}.csv",
            mime="text/csv",
            use_container_width=True,
            key="audit_export_btn",
        )


def _render_history_sidebar(email: str) -> None:
    """Show the last 30 prompts with reuse buttons. Container-agnostic."""
    entries = get_entries(email)
    st.markdown("**Command History**")
    if not entries:
        st.caption("No history yet. Generate something to start building your library.")
        return

    for i, entry in enumerate(entries[:30]):
        preview = entry["input"][:52] + ("…" if len(entry["input"]) > 52 else "")
        badge = f"`{entry['operation_type']}` · `{entry['resource_type']}`"
        ts = entry.get("timestamp", "")[:10]

        with st.container():
            col_text, col_btn = st.columns([5, 1])
            with col_text:
                st.caption(f"{badge}  {ts}")
                st.markdown(f'<span class="tf-sidebar-preview">{preview}</span>', unsafe_allow_html=True)
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
_audit.configure(
    github_token=_get_secret("GITHUB_TOKEN"),
    github_repo=_get_secret("GITHUB_REPO"),
)
_cost.configure(
    github_token=_get_secret("GITHUB_TOKEN"),
    github_repo=_get_secret("GITHUB_REPO"),
)
_rotation.configure(
    github_token=_get_secret("GITHUB_TOKEN"),
    github_repo=_get_secret("GITHUB_REPO"),
)
_user_prefs.configure(
    github_token=_get_secret("GITHUB_TOKEN"),
    github_repo=_get_secret("GITHUB_REPO"),
)
# Feedback issues land in FEEDBACK_REPO if set, otherwise the same repo as
# audit / cost. Lets a deployment route customer feedback to a separate
# product repo without touching the per-user audit destination.
_feedback.configure(
    github_token=_get_secret("GITHUB_TOKEN"),
    feedback_repo=_get_secret("FEEDBACK_REPO") or _get_secret("GITHUB_REPO"),
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

# ── Session-idle timeout (30 minutes) ────────────────────────────────────
# Streamlit has no native session timeout. Track activity in session_state
# and force re-login when idle past the threshold. Updated on every render.
import time as _time

SESSION_IDLE_TIMEOUT_SECONDS = 30 * 60  # 30 minutes
_now = _time.time()
_last = st.session_state.get("last_activity_ts")
if _last and (_now - _last) > SESSION_IDLE_TIMEOUT_SECONDS:
    _audit.log(st.user.email, "session_timeout")
    # Wipe everything except the audit_signin_logged flag (which would
    # cause a duplicate sign_in log), and the env_context which is
    # already cached. Force re-login by logging out.
    for k in list(st.session_state.keys()):
        if k not in ("env_context",):
            del st.session_state[k]
    st.warning("Session timed out after 30 minutes of inactivity. Please sign in again.")
    st.button("Sign in again", on_click=st.logout)
    st.stop()
st.session_state["last_activity_ts"] = _now

# Log sign-in once per session (the first time the user reaches authed state).
if not st.session_state.get("audit_signin_logged"):
    _audit.log(st.user.email, "sign_in")
    st.session_state["audit_signin_logged"] = True

# Phase 8B B.2: first-time-user guided tour. No-op for returning users
# (the seen-flag lives in user_prefs, GitHub-backed). Rendered after
# auth + sign-in logging so the first-load audit ordering is preserved.
render_onboarding_tour(st.user.email)


def _signout_with_audit():
    _audit.log(st.user.email, "sign_out")
    st.logout()


def _render_identity_strip(email: str) -> None:
    """Always-visible identity strip at the top of the sidebar: role pill +
    today's cost / quota progress. Container-agnostic; the new layout
    calls this from inside `with st.sidebar:` so writes flow into the
    sidebar directly (no expander wrap)."""
    role = _roles.get_role(email)
    spent = _cost.today_usd(email)
    cap = _roles.daily_quota_usd(email)
    st.markdown(f'<span class="tf-sidebar-role">Role <b>{role}</b></span>', unsafe_allow_html=True)
    if cap == 0:
        st.caption(f"Today: ${spent:.4f} (no cap)")
    else:
        pct = min(1.0, spent / cap) if cap else 0.0
        st.caption(f"Today: ${spent:.4f} of ${cap:.2f}")
        st.progress(pct)


def _render_admin_block(email: str) -> None:
    """Admin-only block: per-session redaction toggle + stale-secret warnings.
    Container-agnostic; the new layout calls this from inside the Admin
    expander. Stale-secrets used to be an inner expander; flattened to
    inline captions because Streamlit 1.56 forbids nested expanders."""
    role = _roles.get_role(email)
    if not _roles.can("manage_roles", role):
        return
    st.checkbox(
        "Disable PII redaction (session)",
        key="redact_disabled",
    )
    stale = _rotation.stale_list()
    if not stale:
        return
    overdue = [s for s in stale if s.get("age_days") is not None]
    if not overdue:
        st.caption(
            f"Add _tftool/secret_rotation.json to track rotation cadence "
            f"({len(stale)} secret(s) untracked)."
        )
        return
    st.markdown(f"**⚠️ {len(overdue)} overdue secret(s)**")
    for s in overdue:
        age = s.get("age_days")
        st.caption(f"`{s['name']}` · {age}d (target {s['target_days']}d)")
    if len(stale) > len(overdue):
        st.caption(
            f"+ {len(stale) - len(overdue)} untracked (no recorded rotation date)."
        )


def _request_cancel():
    """Set the cancel-mid-generation flag. The next refinement pass will
    abort cleanly via the _GenerationCancelled exception. No-op if no
    generation is in flight (the flag is reset at the start of every
    generation)."""
    st.session_state["cancel_requested"] = True


inject_global_css()
inject_keyboard_shortcuts()

# Phase 8B B.3 polish: per-user theme. Reads the saved preference from
# user_prefs (GitHub-backed, with a local fallback) and injects the
# matching data-theme attribute. Defaults to dark for any user without
# a saved preference. The toggle UI lives in the Account modal.
try:
    _saved_theme = _user_prefs.load(st.user.email).get("theme", "dark")
except Exception:
    _saved_theme = "dark"
inject_theme(_saved_theme)

# Live-context fetch happens before sidebar render so the Connections
# group has the env data ready to display.
_load_env_context()

# Sidebar layout: identity strip stays always-visible; everything else
# folds into collapsible groups so the sidebar fits a single viewport.
# Streamlit forbids nested expanders — Examples library is a top-level
# expander on its own (already), and the GCP partial-errors / stale-
# secrets blocks have been flattened to inline captions.
_is_admin = _roles.can("manage_roles", st.user.email)

with st.sidebar:
    st.markdown(f"Signed in as **{st.user.email}**")
    _render_identity_strip(st.user.email)

    with st.expander("Connections", expanded=True):
        _render_env_sidebar()
        st.divider()
        _render_repo_sidebar(
            st.session_state.get("b_persisted_repo") or _get_secret("GITHUB_REPO")
        )

    render_examples_library()

    with st.expander("Activity", expanded=False):
        _render_audit_sidebar(st.user.email)
        st.divider()
        _render_history_sidebar(st.user.email)

    group_label = "Admin" if _is_admin else "Settings"
    with st.expander(group_label, expanded=False):
        if _is_admin:
            _render_admin_block(st.user.email)
            st.divider()
        render_account_links(st.user.email)

    st.button(
        "Cancel generation",
        on_click=_request_cancel,
        help=(
            "Abort the current generation between refinement passes. "
            "Streamlit is single-threaded, so this only takes effect once "
            "the in-flight LLM call returns; it does not interrupt mid-call."
        ),
    )
    st.button("Sign out", on_click=_signout_with_audit)

# Render whichever modal flag is set (account / help / pricing). Done
# once per run after the sidebar has had its chance to set flags.
render_account_dialogs(st.user.email)

# Top-of-page status row + GCP partial-error banner
render_env_pills(st.session_state.env_context or {})
render_gcp_partial_warning(st.session_state.env_context or {})

# Empty-state hero with starter chips, only on first load
if (
    st.session_state.outputs is None
    and st.session_state.intent is None
    and not st.session_state.parse_error
):
    render_hero_starters()

st.title("Okta + AWS + GCP Terraform Generator")
st.caption("Describe an operation in plain English and get production-ready Terraform HCL plus Lambda or Cloud Function code.")

# Stage 1 — Input
with st.container():
    okta_types, aws_types, gcp_types = render_resource_type_selector()
    render_mode_chip(okta_types, aws_types, gcp_types)
    user_input = st.text_area(
        "Describe the operation",
        placeholder='e.g. "Create a SAML app for Google Workspace with SCIM provisioning" or "Build a Lambda that fires when a user is deactivated in Okta"',
        height=100,
        key="user_input_area",
    )
    _can_parse = _roles.can("parse", st.user.email)
    _can_generate = _roles.can("generate", st.user.email)
    if not _can_generate:
        st.caption(
            f"Your role ({_roles.get_role(st.user.email)}) is read-only. "
            "Parsing is allowed; generation requires contributor or higher."
        )
    parse_clicked = st.button(
        "Parse Intent",
        type="primary",
        disabled=not _can_parse,
        help=("" if _can_parse else "Your role does not permit parsing prompts."),
    )

if parse_clicked and user_input.strip():
    if not _roles.can("parse", st.user.email):
        st.error("Your role does not permit parsing prompts. Contact an admin.")
        st.stop()
    if _quota_block_or_warn():
        st.stop()
    # Redact PII / secrets BEFORE handing the prompt to the LLM. Admins can
    # opt out per session via a sidebar toggle (see _render_role_and_cost_sidebar).
    raw_input = user_input.strip()
    if st.session_state.get("redact_disabled") and _roles.can("manage_roles", st.user.email):
        cleaned_input, redact_summary = raw_input, {}
    else:
        cleaned_input, redact_summary = _redact.redact(raw_input)
    if redact_summary:
        st.info(f"Redacted before sending to Claude: {_redact.format_summary(redact_summary)}.")
        _audit.log(
            st.user.email,
            "redact",
            extra={"summary": redact_summary, "preview": cleaned_input[:200]},
        )
    st.session_state.parse_error = None
    st.session_state.intent = None
    st.session_state.outputs = None
    st.session_state.commit_url = None
    st.session_state.validation_result = None
    st.session_state.last_user_input = cleaned_input  # store the redacted version going forward
    client = _get_client()
    model = _get_model("claude-haiku-4-5-20251001")
    with st.spinner("Parsing intent..."):
        try:
            intent = parse_intent(cleaned_input, client, model=model, resource_type_hints=okta_types)
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
                if gcp_types:
                    intent["gcp_resource_types"] = gcp_types
                if gcp_types and okta_types:
                    intent["output_mode"] = "Okta + GCP"
                elif gcp_types:
                    intent["output_mode"] = "GCP only"
                elif aws_types:
                    intent["output_mode"] = "Both"
                else:
                    intent["output_mode"] = "Okta Terraform only"
                errors = validate_intent(intent)
                if errors:
                    st.session_state.parse_error = "Validation errors: " + "; ".join(errors)
                else:
                    st.session_state.intent = intent
                    _audit.log(
                        st.user.email,
                        "parse_intent",
                        resource_type=intent.get("resource_type", ""),
                        output_mode=intent.get("output_mode", ""),
                        redacted_input_preview=cleaned_input,
                    )
        except ValueError as e:
            st.session_state.parse_error = str(e)

if st.session_state.parse_error:
    render_error_panel(
        "Could not parse this prompt",
        st.session_state.parse_error,
        retry_hint=(
            "Rephrase to name a specific Okta / AWS / GCP resource (group, "
            "app, event hook, Lambda, Pub/Sub topic, etc.) or tick the "
            "matching checkbox above to constrain the resource type."
        ),
    )

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
    if not _roles.can("generate", st.user.email):
        st.error("Your role does not permit generation. Contact an admin.")
        st.stop()
    if _quota_block_or_warn():
        st.stop()
    client = _get_client()
    model = _get_model("claude-haiku-4-5-20251001")
    outputs = _generate_and_refine(st.session_state.intent, "", client, model)
    if outputs is not None:
        st.session_state.outputs = outputs
        _push_output_version(outputs, st.session_state.intent)
        add_entry(st.user.email, st.session_state.last_user_input, st.session_state.intent)
        _audit.log(
            st.user.email,
            "generate",
            resource_type=st.session_state.intent.get("resource_type", ""),
            output_mode=st.session_state.intent.get("output_mode", ""),
            redacted_input_preview=st.session_state.last_user_input,
        )

if st.session_state.gen_error:
    render_error_panel(
        "Generation failed",
        st.session_state.gen_error,
        retry_hint=(
            "Click Parse Intent again, then Generate. Most failures are "
            "transient (Anthropic rate limits, network blips). Persistent "
            "failures usually indicate a malformed intent — open Intent vs "
            "output once you have any output to compare against."
        ),
    )

# Stage 4 — Display + actions
if st.session_state.outputs:
    mode = st.session_state.output_mode
    history = st.session_state.get("b_output_history") or []
    # Version switcher: lets the user flip between the last three generations
    # without losing earlier work. No-op when history has fewer than 2 entries.
    new_active = render_version_switcher(history, st.session_state.get("b_active_version", 0))
    if new_active != st.session_state.get("b_active_version", 0):
        st.session_state["b_active_version"] = new_active
        st.rerun()
    # Display the active version's outputs (defaults to whatever's currently
    # in st.session_state.outputs when history is empty, which preserves the
    # original code path for any first-load flow that bypasses the helper).
    if history and 0 <= new_active < len(history):
        display_outputs = history[new_active]["outputs"]
        display_intent = history[new_active].get("intent") or st.session_state.intent
    else:
        display_outputs = st.session_state.outputs
        display_intent = st.session_state.intent
    # Phase 8B B.2: side-by-side intent vs output for quick interpret-check.
    render_intent_output_compare(display_intent, display_outputs)
    render_code_panels(display_outputs, mode)
    # Phase 8B B.3: feedback widget. Posts a GitHub issue on submit; renders
    # nothing when feedback is not configured.
    render_feedback_widget(display_intent, display_outputs, st.session_state.last_user_input, st.user.email)
    # Diff viewer between the active version and the next-older one. Only
    # renders when the user is looking at the current version (active=0)
    # and at least one prior version exists.
    if new_active == 0 and len(history) >= 2:
        render_diff_viewer(history[1]["outputs"], history[0]["outputs"])
    render_optional_tf(display_outputs.get("optional_tf", ""))
    render_tfvars_example(display_outputs.get("terraform_tfvars_example", ""))

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
        if check_clicked:
            _audit.log(
                st.user.email,
                "self_check",
                resource_type=st.session_state.intent.get("resource_type", ""),
                output_mode=st.session_state.output_mode or "",
                extra={
                    "tf_issues": len(st.session_state.validation_result.get("terraform_issues", [])),
                    "lambda_issues": len(st.session_state.validation_result.get("lambda_issues", [])),
                },
            )
        if fix_clicked:
            _audit.log(
                st.user.email,
                "fix_issues",
                resource_type=st.session_state.intent.get("resource_type", ""),
                output_mode=st.session_state.output_mode or "",
            )
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
                    _push_output_version(fixed, st.session_state.intent)
                    st.session_state.validation_result = None
                    st.session_state.commit_url = None
                    st.rerun()
                except GenerationError as e:
                    st.error(f"Fix failed: {e}")
                    with st.expander("Raw response from Claude"):
                        st.code(e.raw_response)

    default_repo = st.session_state.get("b_persisted_repo") or _get_secret("GITHUB_REPO")
    auto_basename = derive_basename_from_intent(st.session_state.intent)
    push_clicked, regenerate_clicked, extra_instructions, repo_override, branch_override, file_basename = render_action_buttons(
        st.session_state.outputs, mode, default_repo, auto_basename=auto_basename
    )
    # Mirror sidebar behaviour: a repo edit in the push panel also persists.
    if repo_override and repo_override != (st.session_state.get("b_persisted_repo") or ""):
        st.session_state.b_persisted_repo = repo_override

    # Regenerate with automatic 3-pass refinement
    if regenerate_clicked:
        if not _roles.can("regenerate", st.user.email):
            st.error("Your role does not permit regeneration.")
            st.stop()
        if _quota_block_or_warn():
            st.stop()
        st.session_state.gen_error = None
        client = _get_client()
        model = _get_model("claude-haiku-4-5-20251001")
        outputs = _generate_and_refine(st.session_state.intent, extra_instructions, client, model)
        if outputs is not None:
            st.session_state.outputs = outputs
            _push_output_version(outputs, st.session_state.intent)
            st.session_state.commit_url = None
            st.session_state.validation_result = None
            _audit.log(
                st.user.email,
                "regenerate",
                resource_type=st.session_state.intent.get("resource_type", ""),
                output_mode=st.session_state.intent.get("output_mode", ""),
                redacted_input_preview=st.session_state.last_user_input,
                extra={"extra_instructions": (extra_instructions or "")[:200]},
            )
            st.rerun()

    # GitHub push
    if push_clicked:
        github_token = _get_secret("GITHUB_TOKEN")
        if not github_token:
            st.error("GITHUB_TOKEN must be configured in secrets to push to GitHub.")
        elif not repo_override:
            st.error("Repository name is required to push to GitHub.")
        elif not _roles.can_push_to(st.user.email, repo_override):
            role = _roles.get_role(st.user.email)
            st.error(
                f"Role '{role}' cannot push to {repo_override}. "
                "Contributors can push only to repos owned by them; ask an editor or admin."
            )
            _audit.log(st.user.email, "push_blocked", extra={"role": role, "repo": repo_override})
        else:
            files = _build_files(st.session_state.outputs, mode, base=file_basename)
            commit_message = build_commit_message(st.session_state.intent)
            with st.spinner("Pushing to GitHub..."):
                try:
                    commit_url = push_to_github(
                        files, repo_override, github_token, commit_message, branch=branch_override
                    )
                    st.session_state.commit_url = commit_url
                    _audit.log(
                        st.user.email,
                        "push",
                        resource_type=st.session_state.intent.get("resource_type", ""),
                        output_mode=mode,
                        commit_url=commit_url,
                        extra={"repo": repo_override, "branch": branch_override or "default", "file_count": len(files)},
                    )
                except RuntimeError as e:
                    render_error_panel(
                        "GitHub push failed",
                        str(e),
                        retry_hint=(
                            "Verify that GITHUB_TOKEN has `repo` scope and "
                            "write access to the destination repo + branch. "
                            "Then click Push to GitHub again."
                        ),
                        docs_url="https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens",
                    )
                    _audit.log(st.user.email, "push_failed", extra={"error": str(e)[:200]})
                except Exception as e:
                    render_error_panel(
                        "GitHub push failed",
                        str(e),
                        retry_hint=(
                            "This is an unexpected error from the GitHub API. "
                            "Check the token scope and retry; if the error "
                            "persists, copy the message above and email support."
                        ),
                    )
                    _audit.log(st.user.email, "push_failed", extra={"error": str(e)[:200]})

# Stage 5 — Commit URL
if st.session_state.commit_url:
    mode = st.session_state.output_mode or "Both"
    files = _build_files(st.session_state.outputs or {}, mode)
    if render_success_card(st.session_state.commit_url, mode, len(files)):
        st.session_state.intent = None
        st.session_state.outputs = None
        st.session_state.commit_url = None
        st.session_state.validation_result = None
        st.session_state.parse_error = None
        st.rerun()
