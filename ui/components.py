import difflib
import io
import re
import zipfile
import streamlit as st

from ui.css import pill, mode_chip_html  # mode_chip_html retained for any external callers; render_mode_chip itself removed in Phase 16


def _count_resources(hcl: str) -> int:
    """Return the number of top-level `resource "..." "..."` blocks in an HCL
    string. Used by the output-tab badges so the tab label reads `okta.tf (4)`
    without re-parsing the whole file."""
    if not hcl:
        return 0
    return len(re.findall(r'^\s*resource\s+"', hcl, re.MULTILINE))

OUTPUT_MODES = ["Both", "Okta Terraform only", "Lambda only", "GCP only", "Okta + GCP"]


# Starter chips shown on the empty state. Each chip prefills the user-input
# textarea via session_state so they read as suggestions, not commitments.
_STARTER_CHIPS = [
    ("Okta",     "Create a SAML app for Salesforce with attribute statements for department and manager"),
    ("AWS",      "Build a Lambda that fires when a user is added to the Offboarding group and sends an SNS alert"),
    ("GCP",      "Create a Pub/Sub topic called orders that fans out to two Cloud Functions"),
    ("Composite", "Create a new GCP project, a service account, an API key, and enable Vertex AI"),
]


def _infer_mode(
    okta_types: list[str],
    aws_types: list[str],
    gcp_types: list[str],
    jamf_types: list[str] | None = None,
    fleet_types: list[str] | None = None,
    snowflake_types: list[str] | None = None,
    kandji_types: list[str] | None = None,
) -> str:
    """Mirror app.py's mode-inference logic so the read-only chip stays in sync.

    Order: Kandji -> Snowflake -> Fleet GitOps -> JAMF -> GCP -> AWS -> Okta fallback.
    Fleet TF stays CLI/HTTP-only, so the UI never infers a Fleet TF mode."""
    jamf_types = jamf_types or []
    fleet_types = fleet_types or []
    snowflake_types = snowflake_types or []
    kandji_types = kandji_types or []
    if kandji_types and okta_types:
        return "Okta + Kandji"
    if kandji_types:
        return "Kandji only"
    if snowflake_types and okta_types:
        return "Okta + Snowflake"
    if snowflake_types:
        return "Snowflake only"
    if fleet_types and okta_types:
        return "Okta + Fleet GitOps"
    if fleet_types:
        return "Fleet GitOps only"
    if jamf_types and okta_types:
        return "Okta + JAMF"
    if jamf_types:
        return "JAMF only"
    if gcp_types and okta_types:
        return "Okta + GCP"
    if gcp_types:
        return "GCP only"
    if aws_types and okta_types:
        return "Both"
    if aws_types:
        return "Lambda only"
    return "Okta Terraform only"


def render_hero_starters() -> None:
    """Render the empty-state hero block + starter chips. Call only when no
    outputs and no parse error are present. Chips prefill the textarea via
    session_state.user_input_area on click; they do not auto-parse.
    """
    st.markdown(
        '<div class="tf-hero">'
        '<h1>Plain English to deployable Terraform</h1>'
        '<p>Across Okta, AWS Lambda, and GCP. One prompt, one click, one PR.</p>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.caption("Try one to start:")
    cols = st.columns(len(_STARTER_CHIPS))
    for i, (label, prompt) in enumerate(_STARTER_CHIPS):
        with cols[i]:
            if st.button(label, key=f"starter_{label.lower()}", use_container_width=True, help=prompt):
                st.session_state["user_input_area"] = prompt
                # Reset any in-flight intent / outputs so the user starts fresh.
                st.session_state["intent"] = None
                st.session_state["outputs"] = None
                st.session_state["parse_error"] = None
                st.session_state["validation_result"] = None
                st.session_state["commit_url"] = None
                st.rerun()


# render_mode_chip was removed in Phase 16. The read-only mode pill it
# rendered has been replaced by render_output_mode_picker, which is an
# interactive st.selectbox combining the chip's display role with the new
# explicit-override picker. `_infer_mode` is still exported for use by
# `render_output_mode_picker` as the default-value computation when the
# picker is on "Auto".


def render_env_pills(env_context: dict) -> None:
    """Render a horizontal row of Okta / AWS / GCP / JAMF / Fleet / Snowflake /
    Kandji status pills at the top of the main panel. Tooltip exposes resource
    counts. Pure presentation."""
    okta = (env_context or {}).get("okta", {})
    aws = (env_context or {}).get("aws", {})
    gcp = (env_context or {}).get("gcp", {})
    jamf = (env_context or {}).get("jamf", {})
    fleet = (env_context or {}).get("fleet", {})

    pills = []

    # Okta
    if okta.get("connected"):
        n_groups = len(okta.get("groups", []))
        n_apps = len(okta.get("apps", []))
        n_hooks = len(okta.get("event_hooks", []))
        tooltip = f"{n_groups} groups, {n_apps} apps, {n_hooks} event hooks"
        pills.append(pill(f"Okta ({n_groups + n_apps + n_hooks})", "on", tooltip))
    else:
        pills.append(pill("Okta", "off", okta.get("error", "Not configured")))

    # AWS
    if aws.get("connected"):
        n_fns = len(aws.get("lambda_functions", []))
        n_roles = len(aws.get("iam_roles", []))
        tooltip = f"{n_fns} lambdas, {n_roles} roles"
        pills.append(pill(f"AWS ({n_fns + n_roles})", "on", tooltip))
    else:
        pills.append(pill("AWS", "off", aws.get("error", "Not configured")))

    # GCP, with warn state when partial errors are present
    if gcp.get("connected"):
        n_fns = len(gcp.get("functions", []))
        n_sa = len(gcp.get("service_accounts", []))
        n_topics = len(gcp.get("pubsub_topics", []))
        partial = gcp.get("partial_errors") or []
        tooltip = f"{n_fns} functions, {n_sa} SAs, {n_topics} topics"
        state = "warn" if partial else "on"
        if partial:
            tooltip += f" ({len(partial)} services unavailable)"
        pills.append(pill(f"GCP ({n_fns + n_sa + n_topics})", state, tooltip))
    else:
        pills.append(pill("GCP", "off", gcp.get("error", "Not configured")))

    # JAMF, with warn state when partial errors are present (mirrors GCP)
    if jamf.get("connected"):
        n_pol = len(jamf.get("policies", []))
        n_sg = len(jamf.get("smart_groups", []))
        n_scr = len(jamf.get("scripts", []))
        partial = jamf.get("partial_errors") or []
        tooltip = f"{n_pol} policies, {n_sg} smart groups, {n_scr} scripts"
        state = "warn" if partial else "on"
        if partial:
            tooltip += f" ({len(partial)} endpoints unavailable)"
        pills.append(pill(f"JAMF ({n_pol + n_sg + n_scr})", state, tooltip))
    else:
        pills.append(pill("JAMF", "off", jamf.get("error", "Not configured")))

    # Fleet, with warn state when partial errors are present
    if fleet.get("connected"):
        n_lab = len(fleet.get("labels", []))
        n_pol = len(fleet.get("policies", []))
        n_q = len(fleet.get("queries", []))
        n_t = len(fleet.get("teams", []))
        team_policies = fleet.get("team_policies", {}) or {}
        n_tp = sum(len(v) for v in team_policies.values())
        partial = fleet.get("partial_errors") or []
        if n_tp:
            tooltip = (
                f"{n_lab} labels, {n_pol} global policies, "
                f"{n_tp} team policies, {n_q} queries, {n_t} teams"
            )
        else:
            tooltip = (
                f"{n_lab} labels, {n_pol} global policies, "
                f"{n_q} queries, {n_t} teams"
            )
        state = "warn" if partial else "on"
        if partial:
            tooltip += f" ({len(partial)} endpoints unavailable)"
        total = n_lab + n_pol + n_tp + n_q + n_t
        pills.append(pill(f"Fleet ({total})", state, tooltip))
    else:
        pills.append(pill("Fleet", "off", fleet.get("error", "Not configured")))

    # Snowflake (Phase 19c lit the live-context fetcher; pill flips to `on`
    # when SNOWFLAKE_* secrets are configured and the key-pair auth succeeds).
    snowflake = (env_context or {}).get("snowflake", {})
    if snowflake.get("connected"):
        n_wh = len(snowflake.get("warehouses", []))
        n_db = len(snowflake.get("databases", []))
        n_role = len(snowflake.get("roles", []))
        n_user = len(snowflake.get("users", []))
        partial = snowflake.get("partial_errors") or []
        tooltip = f"{n_wh} warehouses, {n_db} databases, {n_role} roles, {n_user} users"
        state = "warn" if partial else "on"
        if partial:
            tooltip += f" ({len(partial)} endpoints unavailable)"
        pills.append(pill(f"Snowflake ({n_wh + n_db + n_role + n_user})", state, tooltip))
    else:
        pills.append(pill("Snowflake", "off", snowflake.get("error", "Not configured")))

    # Kandji / Iru (Phase 23 added the live-context fetcher; pill flips to `on`
    # when KANDJI_* secrets are configured and the bearer-auth succeeds).
    kandji = (env_context or {}).get("kandji", {})
    if kandji.get("connected"):
        n_bp = len(kandji.get("blueprints", []))
        n_li = len(kandji.get("library_items", []))
        n_tg = len(kandji.get("tags", []))
        partial = kandji.get("partial_errors") or []
        tooltip = f"{n_bp} blueprints, {n_li} library items, {n_tg} tags"
        state = "warn" if partial else "on"
        if partial:
            tooltip += f" ({len(partial)} endpoints unavailable)"
        pills.append(pill(f"Kandji ({n_bp + n_li + n_tg})", state, tooltip))
    else:
        pills.append(pill("Kandji", "off", kandji.get("error", "Not configured")))

    st.markdown(f'<div class="tf-pill-row">{"".join(pills)}</div>', unsafe_allow_html=True)


def render_gcp_partial_warning(env_context: dict) -> None:
    """Promote GCP partial errors from a sidebar caption to a top-of-page
    warning when present. No-op when there are no partial errors.
    """
    gcp = (env_context or {}).get("gcp", {})
    partial = gcp.get("partial_errors") or []
    if not partial:
        return
    summary = ", ".join(p.split(":")[0] for p in partial[:3])
    extra = f" (+{len(partial) - 3} more)" if len(partial) > 3 else ""
    st.warning(
        f"GCP live context is partial: {summary}{extra}. "
        "Generation will use placeholder vars for these services. "
        "Check API enablement and SA roles, or click Refresh environment."
    )


def render_success_card(commit_url: str, mode: str, file_count: int) -> bool:
    """Render the post-commit success card. Returns True if "Generate another"
    was clicked, in which case the caller should reset the relevant state keys.
    """
    st.markdown(
        f'''<div class="tf-success-card">
        <div class="title">Pushed to GitHub</div>
        <div class="meta">Mode: <b>{mode}</b> · Files: <b>{file_count}</b></div>
        </div>''',
        unsafe_allow_html=True,
    )
    col_a, col_b = st.columns([1, 1])
    with col_a:
        st.link_button("View commit", commit_url, use_container_width=True)
    with col_b:
        return st.button("Generate another", use_container_width=True, key="generate_another_btn")

_RESOURCE_LABEL_TO_TF = {
    "Workflow": "okta_event_hook",
    "Rule": "okta_group_rule",
    "Group": "okta_group",
    "Policy": "okta_auth_server_policy",
    "User Object": "okta_user_profile_mapping",
    "Network Zone": "okta_network_zone",
    "Brand": "okta_brand",
    "MFA Factor": "okta_factor",
}

_APP_TYPE_TO_TF = {
    "SAML 2.0": "okta_app_saml",
    "OAuth / OIDC": "okta_app_oauth",
}

_AWS_RESOURCE_LABEL_TO_TF = {
    "Lambda": "aws_lambda_function",
    "EventBridge": "aws_cloudwatch_event_rule",
    "API Gateway": "aws_api_gateway_rest_api",
    "Lambda URL": "aws_lambda_function_url",
    "SNS": "aws_sns_topic",
}

_GCP_RESOURCE_LABEL_TO_TF = {
    "Cloud Function": "google_cloudfunctions2_function",
    "Cloud Run": "google_cloud_run_v2_service",
    "Pub/Sub": "google_pubsub_topic",
    "Scheduler": "google_cloud_scheduler_job",
    "GCS Bucket": "google_storage_bucket",
    "Secret": "google_secret_manager_secret",
}

_JAMF_RESOURCE_LABEL_TO_TF = {
    "Policy": "jamfpro_policy",
    "Script": "jamfpro_script",
    "Smart Group": "jamfpro_smart_computer_group_v2",
    "Static Group": "jamfpro_static_computer_group",
    "Config Profile": "jamfpro_macos_configuration_profile_plist_generator",
    "Package": "jamfpro_package",
    "Restricted SW": "jamfpro_restricted_software",
    "Extension Attr": "jamfpro_computer_extension_attribute",
}

_FLEET_RESOURCE_LABEL_TO_TF = {
    "Policy": "fleet_policy",
    "Label": "fleet_label",
    "Query": "fleet_query",
    "Config Profile": "fleet_configuration_profile",
    "Script": "fleet_script",
    "Software": "fleet_software_package",
    "Agent Opts": "fleet_agent_options",
    "Team Settings": "fleet_team_settings",
}

_SNOWFLAKE_RESOURCE_LABEL_TO_TF = {
    "Warehouse": "snowflake_warehouse",
    "Database": "snowflake_database",
    "Schema": "snowflake_schema",
    "Role": "snowflake_role",
    "User": "snowflake_user",
    "Role Grant": "snowflake_grant_account_role",
    "Privilege Grant": "snowflake_grant_privileges_to_account_role",
    "Resource Monitor": "snowflake_resource_monitor",
    "Network Policy": "snowflake_network_policy",
    "SCIM": "snowflake_scim_integration",
}

_KANDJI_RESOURCE_LABEL_TO_TF = {
    "Blueprint": "iru_blueprint",
    "Routing": "iru_blueprint_routing",
    "Library Attach": "iru_blueprint_library_item",
    "Custom Script": "iru_custom_script",
    "Custom Profile": "iru_custom_profile",
    "Custom App": "iru_custom_app",
    "In-house App": "iru_in_house_app",
    "Tag": "iru_tag",
    "Device Note": "iru_device_note",
    "ADE Integration": "iru_ade_integration",
    "ADE Device": "iru_ade_device",
}


def _render_checkbox_grid(
    labels: list[str],
    label_to_tf: dict[str, str],
    key_prefix: str,
    cols_per_row: int = 4,
) -> list[str]:
    """Render a wrapping grid of checkboxes inside the current container.

    Used inside each provider tab. `cols_per_row` controls wrap width — 4 columns
    fits cleanly on a 1280px viewport without horizontal crowding. Returns the
    Terraform resource-type strings for boxes the user has checked.

    Widget `key` values are stable across the Phase 16 tab rewrite so existing
    session-state selections survive the migration.
    """
    selected: list[str] = []
    rows = [labels[i:i + cols_per_row] for i in range(0, len(labels), cols_per_row)]
    for row in rows:
        cols = st.columns(cols_per_row)
        for col_idx, label in enumerate(row):
            with cols[col_idx]:
                key = f"rsel_{key_prefix}_{label.lower().replace(' ', '_').replace('/', '_')}" if key_prefix else f"rsel_{label.lower().replace(' ', '_')}"
                if st.checkbox(label, key=key):
                    selected.append(label_to_tf[label])
    return selected


def _render_okta_tab() -> list[str]:
    """Okta tab body. Resource checkboxes + Application sub-selector with app-type
    radio (SAML vs OAuth/OIDC). Keeps the existing `rsel_application` and
    `rsel_app_type` widget keys for state continuity across the Phase 16 migration."""
    okta_labels = list(_RESOURCE_LABEL_TO_TF.keys())
    selected = _render_checkbox_grid(okta_labels, _RESOURCE_LABEL_TO_TF, key_prefix="", cols_per_row=4)
    app_checked = st.checkbox("Application", key="rsel_application")
    if app_checked:
        app_type = st.radio(
            "Application type",
            options=list(_APP_TYPE_TO_TF.keys()),
            horizontal=True,
            key="rsel_app_type",
            label_visibility="collapsed",
        )
        selected.append(_APP_TYPE_TO_TF[app_type])
    return selected


def render_resource_type_selector() -> tuple[list[str], list[str], list[str], list[str], list[str], list[str], list[str]]:
    """Provider-tabbed resource-type selector. Returns (okta_types, aws_types,
    gcp_types, jamf_types, fleet_types, snowflake_types, kandji_types).

    Phase 16 redesign: replaced the six horizontally-stacked rows (~50 checkboxes
    visible at once) with `st.tabs` so only the active provider's checkboxes
    render. Tab switches are cheap because Streamlit's session state retains all
    checkbox values regardless of which tab body is currently visible, so the
    7-tuple return always reflects the user's full selection across every
    provider.

    Provider routing is unchanged:
      - JAMF -> terraform_jamf_hcl
      - Fleet -> fleet_gitops_yaml (Fleet TF stays CLI/HTTP-only)
      - Snowflake -> terraform_snowflake_hcl via snowflakedb/snowflake ~> 2.0
      - Kandji -> terraform_kandji_hcl via MScottBlake/iru ~> 0.0
    """
    okta_tab, aws_tab, gcp_tab, jamf_tab, fleet_tab, snowflake_tab, kandji_tab = st.tabs(
        ["Okta", "AWS", "GCP", "JAMF", "Fleet", "Snowflake", "Kandji"]
    )

    with okta_tab:
        okta_selected = _render_okta_tab()

    with aws_tab:
        aws_selected = _render_checkbox_grid(
            list(_AWS_RESOURCE_LABEL_TO_TF.keys()),
            _AWS_RESOURCE_LABEL_TO_TF,
            key_prefix="aws",
        )

    with gcp_tab:
        gcp_selected = _render_checkbox_grid(
            list(_GCP_RESOURCE_LABEL_TO_TF.keys()),
            _GCP_RESOURCE_LABEL_TO_TF,
            key_prefix="gcp",
        )

    with jamf_tab:
        jamf_selected = _render_checkbox_grid(
            list(_JAMF_RESOURCE_LABEL_TO_TF.keys()),
            _JAMF_RESOURCE_LABEL_TO_TF,
            key_prefix="jamf",
        )

    with fleet_tab:
        fleet_selected = _render_checkbox_grid(
            list(_FLEET_RESOURCE_LABEL_TO_TF.keys()),
            _FLEET_RESOURCE_LABEL_TO_TF,
            key_prefix="fleet",
        )

    with snowflake_tab:
        snowflake_selected = _render_checkbox_grid(
            list(_SNOWFLAKE_RESOURCE_LABEL_TO_TF.keys()),
            _SNOWFLAKE_RESOURCE_LABEL_TO_TF,
            key_prefix="snowflake",
        )

    with kandji_tab:
        kandji_selected = _render_checkbox_grid(
            list(_KANDJI_RESOURCE_LABEL_TO_TF.keys()),
            _KANDJI_RESOURCE_LABEL_TO_TF,
            key_prefix="kandji",
        )

    return okta_selected, aws_selected, gcp_selected, jamf_selected, fleet_selected, snowflake_selected, kandji_selected


_AUTO_LABEL = "Auto (inferred from selection)"

_ALL_OUTPUT_MODES = [
    _AUTO_LABEL,
    "Okta Terraform only",
    "Lambda only",
    "GCP only",
    "Both",
    "Okta + GCP",
    "JAMF only",
    "Okta + JAMF",
    "Fleet GitOps only",
    "Okta + Fleet GitOps",
    "Fleet TF only",
    "Okta + Fleet TF",
    "Snowflake only",
    "Okta + Snowflake",
    "Kandji only",
    "Okta + Kandji",
]


def render_output_mode_picker(inferred_mode: str) -> str:
    """Render an explicit output-mode dropdown and return the resolved mode.

    Phase 16 replaces the read-only `render_mode_chip` with a `st.selectbox`
    showing all 13 explicit modes plus a sentinel "Auto" entry that defers to
    the inference computed from the user's checkbox selection. Default index
    is 0 (Auto), so first-load behaviour matches the prior chip-based UX. The
    user's pick survives reruns via the `output_mode_picker` session-state key.

    Returns `inferred_mode` when the picker is on Auto, otherwise the literal
    mode string the user selected.
    """
    picked = st.selectbox(
        "Output mode",
        options=_ALL_OUTPUT_MODES,
        index=0,
        key="output_mode_picker",
        help=(
            f"Inferred from your selection: {inferred_mode}. Pick 'Auto' to "
            "track the inferred mode automatically, or pick an explicit mode "
            "to override (e.g. force 'Lambda only' even when Okta resources "
            "are checked)."
        ),
    )
    return inferred_mode if picked == _AUTO_LABEL else picked


def render_intent_card(intent: dict) -> dict | None:
    op = intent.get("operation_type", "create")
    res = intent.get("resource_type", "resource")
    resource_types = intent.get("resource_types", [res])
    name = intent.get("resource_name", "")
    ambiguities = intent.get("ambiguities", [])
    notes = intent.get("notes", [])

    types_display = " · ".join(f"`{rt}`" for rt in resource_types)
    st.markdown(f"**{op.capitalize()}** · {types_display}" + (f" · `{name}`" if name else ""))

    for note in notes:
        st.info(note)

    with st.form("intent_form"):
        provider_version = st.radio(
            "Okta provider version",
            options=["~> 4.0 (tested stable)", "~> 6.0 (current stable)"],
            horizontal=True,
            help="6.x is the current stable release. 4.x is well-tested with this tool. Both are compatible with the generated HCL.",
        )

        if ambiguities:
            st.markdown("**Answer the questions below before generating:**")
            answers = {}
            for q in ambiguities:
                answers[q] = st.text_input(q, placeholder="Your answer (leave blank to let Claude decide)")
        else:
            st.success("No ambiguities — ready to generate.")
            answers = {}

        submitted = st.form_submit_button("Generate")

    if not submitted:
        return None

    pv_constraint = provider_version.split(" ")[0]
    return {**intent, "answers": answers, "provider_version": pv_constraint}


def render_version_switcher(history: list[dict], active_index: int) -> int:
    """Render a horizontal radio above the output panels when more than one
    generation exists in history. Returns the new active_index. When only
    one version exists, returns active_index unchanged and renders nothing.
    history is a list of dicts with key 'ts' (timestamp string)."""
    if len(history) < 2:
        return active_index
    labels = []
    for i, entry in enumerate(history):
        ts = (entry.get("ts") or "")[11:19]  # HH:MM:SS slice
        tag = "current" if i == 0 else f"v-{i}"
        labels.append(f"{tag} · {ts}" if ts else tag)
    safe_index = max(0, min(active_index, len(labels) - 1))
    picked = st.radio(
        "Version",
        options=list(range(len(labels))),
        format_func=lambda i: labels[i],
        index=safe_index,
        horizontal=True,
        key="b_version_radio",
    )
    return picked


def _summarize_resources(hcl: str) -> list[tuple[str, str]]:
    """Extract `resource "TYPE" "NAME"` pairs from an HCL string. Used by the
    intent-vs-output comparison so the user can see at a glance which
    resources Claude actually produced in each file."""
    if not hcl:
        return []
    return re.findall(r'^\s*resource\s+"([^"]+)"\s+"([^"]+)"', hcl, re.MULTILINE)


def render_intent_output_compare(intent: dict, outputs: dict) -> None:
    """Render a collapsed expander showing the parsed intent next to a
    structured summary of what Claude generated. Lets the user verify the
    model interpreted the prompt correctly without scrolling through code.

    Pure presentation; no state mutation. No-op when intent or outputs are
    missing so callers don't need to guard.
    """
    if not intent or not outputs:
        return
    with st.expander("Intent vs output", expanded=False):
        left, right = st.columns(2)
        with left:
            st.caption("Parsed intent (what Claude understood)")
            shown = {
                k: intent.get(k)
                for k in (
                    "operation_type", "resource_type", "resource_types",
                    "resource_name", "output_mode", "provider_version",
                    "aws_resource_types", "gcp_resource_types",
                    "ambiguities", "notes", "answers",
                )
                if intent.get(k) not in (None, "", [], {})
            }
            st.json(shown, expanded=True)
        with right:
            st.caption("Generated resources (what Claude produced)")
            file_keys = [
                ("okta.tf", "terraform_okta_hcl"),
                ("lambda.tf", "terraform_lambda_hcl"),
                ("gcp.tf", "terraform_gcp_hcl"),
            ]
            shown_any = False
            for label, key in file_keys:
                pairs = _summarize_resources(outputs.get(key, "") or "")
                if not pairs:
                    continue
                shown_any = True
                st.markdown(f"**{label}** · {len(pairs)} resource(s)")
                for rtype, rname in pairs:
                    st.markdown(f"· `{rtype}.{rname}`")
            for label, key in (
                ("lambda_function.py", "lambda_python"),
                ("cloud_function.py", "cloud_function_python"),
            ):
                content = (outputs.get(key) or "").strip()
                if content:
                    shown_any = True
                    lines = content.count("\n") + 1
                    st.markdown(f"**{label}** · {lines} line(s)")
            if not shown_any:
                st.caption("No resources generated yet.")


_SUPPORT_EMAIL_FOR_ERRORS = "cyrillical@gmail.com"


def render_error_panel(
    title: str,
    message: str,
    *,
    retry_hint: str | None = None,
    docs_url: str | None = None,
    support_email: str | None = None,
) -> None:
    """Structured error renderer for the high-traffic error sites
    (parse / generate / push). Bundles three things every error needs:
    what went wrong, how to retry, who to contact. Falls back to
    plain st.error if streamlit is unhealthy."""
    try:
        st.error(f"**{title}**\n\n{message}")
    except Exception:
        return
    extras = []
    if retry_hint:
        extras.append(f"**Try:** {retry_hint}")
    if docs_url:
        extras.append(f"**Docs:** [{docs_url}]({docs_url})")
    if extras:
        st.caption("  \n".join(extras))
    contact = support_email or _SUPPORT_EMAIL_FOR_ERRORS
    st.caption(f"Still stuck? Email `{contact}` with the error text above.")


def render_feedback_widget(intent: dict, outputs: dict, user_input: str, email: str) -> None:
    """Render a thumbs-up/down + free-text feedback widget. Submission
    posts a GitHub issue via the feedback module. Renders nothing when
    feedback isn't configured (no FEEDBACK_REPO + GITHUB_TOKEN), so the
    widget silently disappears in dev environments without GitHub.

    State keys: feedback_sentiment, feedback_comment, feedback_submitted_for
    (the request_id-equivalent tied to the current outputs so re-renders
    don't show a stale 'submitted' confirmation across regenerations).
    """
    try:
        import feedback as _fb
    except Exception:
        return
    if not _fb.is_configured():
        return
    if not intent or not outputs:
        return

    output_signature = (
        len(outputs.get("terraform_okta_hcl", "") or "")
        + len(outputs.get("terraform_lambda_hcl", "") or "")
        + len(outputs.get("terraform_gcp_hcl", "") or "")
        + len(outputs.get("lambda_python", "") or "")
        + len(outputs.get("cloud_function_python", "") or "")
    )
    submitted_for = st.session_state.get("feedback_submitted_for")
    if submitted_for == output_signature:
        url = st.session_state.get("feedback_last_url", "")
        msg = "Feedback submitted. Thanks!"
        if url:
            msg += f" [View issue]({url})"
        st.success(msg)
        return

    with st.expander("Was this output helpful?", expanded=False):
        col_up, col_down, _ = st.columns([1, 1, 4])
        with col_up:
            up_clicked = st.button("👍", key="fb_up", help="Helpful — output matched the request")
        with col_down:
            down_clicked = st.button("👎", key="fb_down", help="Not helpful — flag for review")

        if up_clicked:
            st.session_state["feedback_sentiment"] = "up"
        if down_clicked:
            st.session_state["feedback_sentiment"] = "down"

        sentiment = st.session_state.get("feedback_sentiment")
        if sentiment:
            label = "What was good?" if sentiment == "up" else "What went wrong?"
            comment = st.text_area(
                label,
                placeholder="Optional. Specifics help us tune the prompts.",
                height=90,
                key="fb_comment",
            )
            if st.button("Send feedback", key="fb_submit", type="primary"):
                output_summary = {
                    "okta_resources": len(_summarize_resources(outputs.get("terraform_okta_hcl", "") or "")),
                    "lambda_resources": len(_summarize_resources(outputs.get("terraform_lambda_hcl", "") or "")),
                    "gcp_resources": len(_summarize_resources(outputs.get("terraform_gcp_hcl", "") or "")),
                    "lambda_py_lines": (outputs.get("lambda_python", "") or "").count("\n") + 1,
                }
                url = _fb.submit(
                    email=email,
                    sentiment=sentiment,
                    comment=comment or "",
                    intent=intent,
                    user_input=user_input,
                    output_summary=output_summary,
                )
                if url:
                    st.session_state["feedback_submitted_for"] = output_signature
                    st.session_state["feedback_last_url"] = url
                    st.session_state["feedback_sentiment"] = None
                    st.session_state["fb_comment"] = ""
                    st.rerun()
                else:
                    st.error(
                        "Could not post the feedback issue. Check that GITHUB_TOKEN "
                        "has issue-create permission on the configured FEEDBACK_REPO."
                    )


def render_diff_viewer(prev_outputs: dict, curr_outputs: dict) -> None:
    """Render a unified-diff expander showing changes per file between the
    previous and current generation. No-op if either side is empty.

    Truncates each file's diff at 500 lines so a degenerate output can't
    blow up the page render time."""
    if not prev_outputs or not curr_outputs:
        return
    file_keys = [
        ("okta.tf", "terraform_okta_hcl"),
        ("lambda.tf", "terraform_lambda_hcl"),
        ("gcp.tf", "terraform_gcp_hcl"),
        ("lambda_function.py", "lambda_python"),
        ("cloud_function.py", "cloud_function_python"),
    ]
    diffs: list[tuple[str, str]] = []
    for label, key in file_keys:
        a = (prev_outputs.get(key) or "").splitlines(keepends=True)
        b = (curr_outputs.get(key) or "").splitlines(keepends=True)
        if not a and not b:
            continue
        diff_lines = list(difflib.unified_diff(
            a, b, fromfile=f"prev/{label}", tofile=f"curr/{label}", n=2,
        ))
        if not diff_lines:
            continue
        if len(diff_lines) > 500:
            diff_lines = diff_lines[:500] + [f"\n... [truncated, {len(diff_lines) - 500} more lines]\n"]
        diffs.append((label, "".join(diff_lines)))
    if not diffs:
        return
    with st.expander(f"Changes since previous generation ({len(diffs)} file(s))", expanded=False):
        for label, diff_text in diffs:
            st.caption(label)
            st.code(diff_text, language="diff")


def render_code_panels(outputs: dict, mode: str):
    show_okta_tf = mode in ("Both", "Okta Terraform only", "Okta + GCP")
    show_lambda_tf = mode in ("Both", "Lambda only")
    show_lambda_py = mode in ("Both", "Lambda only")
    show_gcp_tf = mode in ("GCP only", "Okta + GCP")
    show_gcp_py = mode in ("GCP only", "Okta + GCP")

    has_tf = show_okta_tf or show_lambda_tf or show_gcp_tf
    has_code = show_lambda_py or show_gcp_py

    if has_tf and has_code:
        left, right = st.columns(2)
        with left:
            _render_terraform(outputs, show_okta_tf, show_lambda_tf, show_gcp_tf)
        with right:
            if show_gcp_py:
                _render_cloud_function(outputs)
            else:
                _render_lambda(outputs)
    elif has_tf:
        _render_terraform(outputs, show_okta_tf, show_lambda_tf, show_gcp_tf)
    elif show_gcp_py:
        _render_cloud_function(outputs)
    else:
        _render_lambda(outputs)


def _render_terraform(outputs: dict, show_okta: bool, show_lambda: bool, show_gcp: bool):
    st.subheader("Terraform")
    tabs_to_show = []
    if show_okta:
        tabs_to_show.append(("okta.tf", outputs.get("terraform_okta_hcl", "")))
    if show_lambda:
        tabs_to_show.append(("lambda.tf", outputs.get("terraform_lambda_hcl", "")))
    if show_gcp:
        tabs_to_show.append(("gcp.tf", outputs.get("terraform_gcp_hcl", "")))
    if not tabs_to_show:
        return
    labels = [
        f"{name} ({_count_resources(content)})" if _count_resources(content) else name
        for name, content in tabs_to_show
    ]
    tabs = st.tabs(labels)
    for tab, (_, content) in zip(tabs, tabs_to_show):
        with tab:
            st.code(content, language="hcl")


def _render_lambda(outputs: dict):
    st.subheader("Lambda Python")
    st.code(outputs["lambda_python"], language="python")
    if outputs.get("lambda_requirements", "").strip():
        with st.expander("Lambda requirements.txt"):
            st.code(outputs["lambda_requirements"], language="text")


def _render_cloud_function(outputs: dict):
    st.subheader("Cloud Function Python")
    st.code(outputs.get("cloud_function_python", ""), language="python")
    if outputs.get("cloud_function_requirements", "").strip():
        with st.expander("Cloud Function requirements.txt"):
            st.code(outputs["cloud_function_requirements"], language="text")


def build_project_zip(outputs: dict, mode: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        if mode in ("Both", "Okta Terraform only", "Okta + GCP"):
            okta_hcl = outputs.get("terraform_okta_hcl", "")
            if okta_hcl.strip():
                zf.writestr("terraform/okta.tf", okta_hcl)
        if mode in ("Both",):
            lambda_hcl = outputs.get("terraform_lambda_hcl", "")
            if lambda_hcl.strip():
                zf.writestr("terraform/lambda.tf", lambda_hcl)
        if mode in ("Both", "Lambda only"):
            zf.writestr("lambda/lambda_function.py", outputs.get("lambda_python", ""))
            zf.writestr("lambda/requirements.txt", outputs.get("lambda_requirements", ""))
        if mode in ("GCP only", "Okta + GCP"):
            gcp_hcl = outputs.get("terraform_gcp_hcl", "")
            if gcp_hcl.strip():
                zf.writestr("terraform/gcp.tf", gcp_hcl)
            zf.writestr("cloud_function/main.py", outputs.get("cloud_function_python", ""))
            zf.writestr("cloud_function/requirements.txt", outputs.get("cloud_function_requirements", ""))
        optional_tf = outputs.get("optional_tf", "")
        if optional_tf and optional_tf.strip():
            zf.writestr("terraform/optional_extensions.tf", optional_tf)
        tfvars = outputs.get("terraform_tfvars_example", "")
        if tfvars and tfvars.strip():
            zf.writestr("terraform/terraform.tfvars.example", tfvars)
    return buffer.getvalue()


def render_tfvars_example(tfvars: str) -> None:
    if not tfvars or not tfvars.strip():
        return
    with st.expander("terraform.tfvars.example — fill in and rename to terraform.tfvars"):
        st.caption("Copy the values below into a file named terraform.tfvars before running terraform apply.")
        st.code(tfvars, language="hcl")


def render_optional_tf(optional_tf: str) -> None:
    if not optional_tf or not optional_tf.strip():
        return
    st.divider()
    with st.expander("Optional extensions — add to your Terraform directory to enable"):
        st.caption(
            "These resources complement the main configuration but are not applied by default. "
            "Copy them into a separate `.tf` file and run `terraform apply` when ready."
        )
        st.code(optional_tf, language="hcl")


_FILE_PREFIX_RE = re.compile(
    r"^(?P<file>okta\.tf|lambda\.tf|gcp\.tf|lambda_function\.py|cloud_function\.py|main\.py)\s*[:\s]",
    re.IGNORECASE,
)
_ERROR_KEYWORDS = ("error", "missing", "fail", "invalid", "unsupported", "duplicate", "forbidden")
_WARN_KEYWORDS = ("warn", "consider", "should", "recommend")


def _infer_severity(issue: str) -> str:
    low = issue.lower()
    if any(k in low for k in _ERROR_KEYWORDS):
        return "error"
    if any(k in low for k in _WARN_KEYWORDS):
        return "warn"
    return "info"


def _bucket_issue(issue: str, default_file: str) -> tuple[str, str]:
    """Return (file_label, cleaned_issue). Detects an inline file prefix
    like `okta.tf: ...` and falls back to `default_file` for the bucket
    when the issue text doesn't name a file."""
    m = _FILE_PREFIX_RE.match(issue.lstrip())
    if m:
        return m.group("file").lower(), issue[m.end():].strip().lstrip(":-").strip()
    return default_file, issue


def render_validation_result(result: dict) -> bool:
    """Renders self-check result grouped by file with severity inference.
    Returns True if Fix Issues was clicked."""
    overall = result.get("overall", "warn")
    tf_issues = result.get("terraform_issues", [])
    lambda_issues = result.get("lambda_issues", [])

    if overall == "pass":
        st.success("Self-check passed: output matches the request with no issues found.")
        return False

    total = len(tf_issues) + len(lambda_issues)
    badge = f"{total} issue(s) flagged"
    if overall == "warn":
        st.warning(badge)
    else:
        st.error(badge)

    # Bucket every issue by inferred filename. Terraform issues default to
    # "terraform" when no file prefix is present; lambda issues default to
    # the python source. Same data as before, better signal.
    buckets: dict[str, list[tuple[str, str]]] = {}
    for issue in tf_issues:
        file_label, clean = _bucket_issue(issue, "terraform")
        buckets.setdefault(file_label, []).append((clean, _infer_severity(clean)))
    for issue in lambda_issues:
        file_label, clean = _bucket_issue(issue, "lambda_function.py")
        buckets.setdefault(file_label, []).append((clean, _infer_severity(clean)))

    file_order = ["okta.tf", "lambda.tf", "gcp.tf", "terraform",
                  "lambda_function.py", "cloud_function.py", "main.py"]
    ordered = [f for f in file_order if f in buckets] + \
              [f for f in buckets if f not in file_order]

    for file_label in ordered:
        items = buckets[file_label]
        with st.expander(f"{file_label} ({len(items)})", expanded=True):
            for clean, sev in items:
                icon = {"error": "✖", "warn": "▲", "info": "·"}.get(sev, "·")
                st.markdown(
                    f'<div><span class="tf-issue-severity-{sev}">{icon}</span> {clean}</div>',
                    unsafe_allow_html=True,
                )

    return st.button("Fix Issues", type="primary")


def render_action_buttons(
    outputs: dict,
    mode: str,
    default_repo: str,
    auto_basename: str = "",
) -> tuple[bool, bool, str, str, str, str]:
    st.divider()

    with st.expander("GitHub push settings"):
        repo_override = st.text_input(
            "Repository (owner/repo)",
            value=default_repo,
            placeholder="cyrillical00/my-repo",
        )
        branch_override = st.text_input(
            "Branch",
            value="main",
            placeholder="main",
        )
        if auto_basename:
            placeholder_text = f"auto-derived from intent: {auto_basename}"
        else:
            placeholder_text = "e.g. hr_portal — leave blank for legacy 'okta.tf'"
        file_basename = st.text_input(
            "Resource basename (optional)",
            value="",
            placeholder=placeholder_text,
            help=(
                "Filename base for the pushed files. When blank, we auto-derive "
                "from the parsed intent's resource_name (so prompt #2 lands at "
                "terraform/hr_portal_workday.tf without you typing anything). "
                "Type something here to override the auto-derived value, or "
                "leave blank for the auto-derive default. If both this and "
                "the auto-derive are empty, the legacy single-file path "
                "(terraform/okta.tf) is used."
            ),
        )

    extra_instructions = st.text_area(
        "Extra instructions for regeneration (optional)",
        placeholder="e.g. add SCIM provisioning config, use Python 3.12 runtime",
        height=80,
    )

    col1, col2, col3 = st.columns(3)

    with col1:
        regenerate_clicked = st.button("Regenerate", use_container_width=True)

    with col2:
        push_clicked = st.button("Push to GitHub", type="primary", use_container_width=True)

    with col3:
        zip_bytes = build_project_zip(outputs, mode)
        st.download_button(
            label="Download as ZIP",
            data=zip_bytes,
            file_name="okta_tf_lambda.zip",
            mime="application/zip",
            use_container_width=True,
        )

    # If the user did not type anything, fall back to the auto-derived basename
    # so per-prompt files always have a stable, unique path. Empty user input
    # AND empty auto_basename means "use legacy okta.tf path" — exactly the
    # behavior that was here before this auto-derive feature.
    effective_basename = file_basename.strip() or auto_basename

    return (
        push_clicked,
        regenerate_clicked,
        extra_instructions,
        repo_override.strip(),
        branch_override.strip(),
        effective_basename,
    )
