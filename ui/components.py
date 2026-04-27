import io
import zipfile
import streamlit as st

OUTPUT_MODES = ["Both", "Okta Terraform only", "Lambda only"]

_RESOURCE_LABEL_TO_TF = {
    "Workflow": "okta_event_hook",
    "Rule": "okta_group_rule",
    "Group": "okta_group",
    "Policy": "okta_auth_server_policy",
    "User Object": "okta_user_profile_mapping",
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


def render_resource_type_selector() -> tuple[list[str], list[str]]:
    """Two-section checkbox selector. Returns (okta_types, aws_types)."""
    okta_labels = list(_RESOURCE_LABEL_TO_TF.keys())
    aws_labels = list(_AWS_RESOURCE_LABEL_TO_TF.keys())
    okta_selected: list[str] = []
    aws_selected: list[str] = []

    # Okta row
    okta_cols = st.columns([0.7] + [1] * (len(okta_labels) + 1))
    with okta_cols[0]:
        st.markdown("**Okta**")
    for i, label in enumerate(okta_labels):
        with okta_cols[i + 1]:
            if st.checkbox(label, key=f"rsel_{label.lower().replace(' ', '_')}"):
                okta_selected.append(_RESOURCE_LABEL_TO_TF[label])
    with okta_cols[-1]:
        app_checked = st.checkbox("Application", key="rsel_application")

    if app_checked:
        app_type = st.radio(
            "Application type",
            options=list(_APP_TYPE_TO_TF.keys()),
            horizontal=True,
            key="rsel_app_type",
            label_visibility="collapsed",
        )
        okta_selected.append(_APP_TYPE_TO_TF[app_type])

    # AWS row
    aws_cols = st.columns([0.7] + [1] * len(aws_labels))
    with aws_cols[0]:
        st.markdown("**AWS**")
    for i, label in enumerate(aws_labels):
        with aws_cols[i + 1]:
            if st.checkbox(label, key=f"rsel_aws_{label.lower().replace(' ', '_')}"):
                aws_selected.append(_AWS_RESOURCE_LABEL_TO_TF[label])

    return okta_selected, aws_selected


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


def render_code_panels(outputs: dict, mode: str):
    show_tf = mode in ("Both", "Okta Terraform only")
    show_lambda = mode in ("Both", "Lambda only")

    if show_tf and show_lambda:
        left, right = st.columns(2)
        with left:
            _render_terraform(outputs)
        with right:
            _render_lambda(outputs)
    elif show_tf:
        _render_terraform(outputs)
    else:
        _render_lambda(outputs)


def _render_terraform(outputs: dict):
    st.subheader("Terraform")
    tf_tab1, tf_tab2 = st.tabs(["okta.tf", "lambda.tf"])
    with tf_tab1:
        st.code(outputs["terraform_okta_hcl"], language="hcl")
    with tf_tab2:
        st.code(outputs["terraform_lambda_hcl"], language="hcl")


def _render_lambda(outputs: dict):
    st.subheader("Lambda Python")
    st.code(outputs["lambda_python"], language="python")
    if outputs.get("lambda_requirements", "").strip():
        with st.expander("Lambda requirements.txt"):
            st.code(outputs["lambda_requirements"], language="text")


def build_project_zip(outputs: dict, mode: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        if mode in ("Both", "Okta Terraform only"):
            zf.writestr("terraform/okta.tf", outputs["terraform_okta_hcl"])
            zf.writestr("terraform/lambda.tf", outputs["terraform_lambda_hcl"])
        if mode in ("Both", "Lambda only"):
            zf.writestr("lambda/lambda_function.py", outputs["lambda_python"])
            zf.writestr("lambda/requirements.txt", outputs.get("lambda_requirements", ""))
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


def render_validation_result(result: dict) -> bool:
    """Renders self-check result. Returns True if Fix Issues was clicked."""
    overall = result.get("overall", "warn")
    tf_issues = result.get("terraform_issues", [])
    lambda_issues = result.get("lambda_issues", [])

    if overall == "pass":
        st.success("Self-check passed — output matches the request with no issues found.")
        return False

    badge = "⚠️ Warning" if overall == "warn" else "❌ Failed"
    st.warning(badge) if overall == "warn" else st.error(badge)

    if tf_issues:
        st.markdown("**Terraform issues:**")
        for issue in tf_issues:
            st.markdown(f"- {issue}")

    if lambda_issues:
        st.markdown("**Lambda issues:**")
        for issue in lambda_issues:
            st.markdown(f"- {issue}")

    return st.button("Fix Issues", type="primary")


def render_action_buttons(outputs: dict, mode: str, default_repo: str) -> tuple[bool, bool, str, str, str, str]:
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
        file_basename = st.text_input(
            "Resource basename (optional)",
            value="",
            placeholder="e.g. hr_portal — leave blank for legacy 'okta.tf'",
            help=(
                "Filename base for generated files. Use distinct names per prompt to "
                "avoid overwriting prior pushes (e.g. 'hr_portal' produces "
                "terraform/hr_portal.tf instead of terraform/okta.tf). Leave blank "
                "to use the legacy fixed paths (terraform/okta.tf, terraform/lambda.tf, "
                "lambda/lambda_function.py)."
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

    return (
        push_clicked,
        regenerate_clicked,
        extra_instructions,
        repo_override.strip(),
        branch_override.strip(),
        file_basename.strip(),
    )
