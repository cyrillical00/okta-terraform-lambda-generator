import io
import zipfile
import streamlit as st

OUTPUT_MODES = ["Both", "Okta Terraform only", "Lambda only"]


def render_intent_card(intent: dict) -> dict | None:
    op = intent.get("operation_type", "create")
    res = intent.get("resource_type", "resource")
    name = intent.get("resource_name", "")
    ambiguities = intent.get("ambiguities", [])
    notes = intent.get("notes", [])

    st.markdown(f"**{op.capitalize()}** · `{res}`" + (f" · `{name}`" if name else ""))

    for note in notes:
        st.info(note)

    with st.form("intent_form"):
        output_mode = st.radio(
            "What do you want to generate?",
            options=OUTPUT_MODES,
            horizontal=True,
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

    return {**intent, "answers": answers, "output_mode": output_mode}


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
    return buffer.getvalue()


def render_action_buttons(outputs: dict, mode: str, default_repo: str) -> tuple[bool, bool, str, str, str]:
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

    return push_clicked, regenerate_clicked, extra_instructions, repo_override.strip(), branch_override.strip()
