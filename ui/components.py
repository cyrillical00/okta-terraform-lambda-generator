import io
import json
import zipfile
import streamlit as st

from generator.parser import ALLOWED_OPERATION_TYPES, ALLOWED_RESOURCE_TYPES


def render_intent_card(intent: dict) -> dict | None:
    with st.form("intent_form"):
        st.subheader("Confirm parsed intent")

        if intent.get("ambiguities"):
            for ambiguity in intent["ambiguities"]:
                st.warning(f"Ambiguity: {ambiguity}")

        if intent.get("notes"):
            for note in intent["notes"]:
                st.info(f"Note: {note}")

        operation_type = st.selectbox(
            "Operation type",
            options=sorted(ALLOWED_OPERATION_TYPES),
            index=sorted(ALLOWED_OPERATION_TYPES).index(intent.get("operation_type", "create"))
            if intent.get("operation_type") in ALLOWED_OPERATION_TYPES
            else 0,
        )

        resource_types = sorted(ALLOWED_RESOURCE_TYPES)
        resource_type = st.selectbox(
            "Resource type",
            options=resource_types,
            index=resource_types.index(intent.get("resource_type", "okta_group"))
            if intent.get("resource_type") in ALLOWED_RESOURCE_TYPES
            else 0,
        )

        resource_name = st.text_input("Resource name (snake_case)", value=intent.get("resource_name", ""))

        attributes_str = st.text_area(
            "Attributes (JSON)",
            value=json.dumps(intent.get("attributes", {}), indent=2),
            height=150,
        )

        submitted = st.form_submit_button("Confirm and Generate")

    if not submitted:
        return None

    try:
        attributes = json.loads(attributes_str)
    except json.JSONDecodeError as e:
        st.error(f"Attributes JSON is invalid: {e}")
        return None

    return {
        **intent,
        "operation_type": operation_type,
        "resource_type": resource_type,
        "resource_name": resource_name,
        "attributes": attributes,
    }


def render_code_panels(outputs: dict):
    left, right = st.columns(2)

    with left:
        st.subheader("Terraform")
        tf_tab1, tf_tab2 = st.tabs(["okta.tf", "lambda.tf"])
        with tf_tab1:
            st.code(outputs["terraform_okta_hcl"], language="hcl")
        with tf_tab2:
            st.code(outputs["terraform_lambda_hcl"], language="hcl")

    with right:
        st.subheader("Lambda Python")
        st.code(outputs["lambda_python"], language="python")
        if outputs.get("lambda_requirements", "").strip():
            with st.expander("Lambda requirements.txt"):
                st.code(outputs["lambda_requirements"], language="text")


def build_project_zip(outputs: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("terraform/okta.tf", outputs["terraform_okta_hcl"])
        zf.writestr("terraform/lambda.tf", outputs["terraform_lambda_hcl"])
        zf.writestr("lambda/lambda_function.py", outputs["lambda_python"])
        zf.writestr("lambda/requirements.txt", outputs.get("lambda_requirements", ""))
    return buffer.getvalue()


def render_action_buttons(outputs: dict) -> tuple[bool, bool, str]:
    st.divider()

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
        zip_bytes = build_project_zip(outputs)
        st.download_button(
            label="Download as ZIP",
            data=zip_bytes,
            file_name="okta_tf_lambda.zip",
            mime="application/zip",
            use_container_width=True,
        )

    return push_clicked, regenerate_clicked, extra_instructions
