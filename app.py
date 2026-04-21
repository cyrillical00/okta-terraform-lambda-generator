import os

import anthropic
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="Okta TF+Lambda Generator",
    page_icon="",
    layout="wide",
)

from generator.parser import parse_intent, validate_intent
from generator.terraform_gen import generate_all, GenerationError
from generator.lambda_gen import validate_lambda_python
from generator.validator import validate_outputs
from gh_push.push import push_to_github, build_commit_message
from ui.components import render_intent_card, render_code_panels, render_action_buttons, render_validation_result


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


def _build_files(outputs: dict, mode: str) -> dict[str, str]:
    files = {}
    if mode in ("Both", "Okta Terraform only"):
        files["terraform/okta.tf"] = outputs["terraform_okta_hcl"]
        files["terraform/lambda.tf"] = outputs["terraform_lambda_hcl"]
    if mode in ("Both", "Lambda only"):
        files["lambda/lambda_function.py"] = outputs["lambda_python"]
        files["lambda/requirements.txt"] = outputs.get("lambda_requirements", "")
    return files


_init_session_state()

st.title("Okta Terraform + Lambda Generator")
st.caption("Describe an Okta operation in plain English and get production-ready Terraform HCL and AWS Lambda Python.")

# Stage 1 — Input
with st.container():
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
            intent = parse_intent(user_input.strip(), client, model=model)
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

# Stage 3 — Generation
if st.session_state.generation_triggered:
    st.session_state.generation_triggered = False
    st.session_state.gen_error = None
    client = _get_client()
    model = _get_model("claude-haiku-4-5-20251001")
    with st.spinner("Generating..."):
        try:
            outputs = generate_all(st.session_state.intent, "", client, model=model)
            syntax_errors = validate_lambda_python(outputs["lambda_python"])
            if syntax_errors:
                st.warning(f"Lambda syntax warning: {'; '.join(syntax_errors)}")
            st.session_state.outputs = outputs
        except GenerationError as e:
            st.session_state.gen_error = str(e)
            with st.expander("Raw response from Claude"):
                st.code(e.raw_response)

if st.session_state.gen_error:
    st.error(st.session_state.gen_error)

# Stage 4 — Display + actions
if st.session_state.outputs:
    mode = st.session_state.output_mode
    render_code_panels(st.session_state.outputs, mode)

    col_check, _ = st.columns([1, 3])
    with col_check:
        check_clicked = st.button("Run Self-Check", use_container_width=True)

    if check_clicked:
        client = _get_client()
        model = _get_model("claude-haiku-4-5-20251001")
        with st.spinner("Running independent review..."):
            st.session_state.validation_result = validate_outputs(
                user_input=st.session_state.get("last_user_input", ""),
                intent=st.session_state.intent,
                outputs=st.session_state.outputs,
                client=client,
                model=model,
            )

    if st.session_state.validation_result:
        render_validation_result(st.session_state.validation_result)

    default_repo = _get_secret("GITHUB_REPO")
    push_clicked, regenerate_clicked, extra_instructions, repo_override, branch_override = render_action_buttons(
        st.session_state.outputs, mode, default_repo
    )

    # Regenerate
    if regenerate_clicked:
        st.session_state.gen_error = None
        client = _get_client()
        model = _get_model("claude-haiku-4-5-20251001")
        with st.spinner("Regenerating..."):
            try:
                outputs = generate_all(st.session_state.intent, extra_instructions, client, model=model)
                syntax_errors = validate_lambda_python(outputs["lambda_python"])
                if syntax_errors:
                    st.warning(f"Lambda syntax warning: {'; '.join(syntax_errors)}")
                st.session_state.outputs = outputs
                st.session_state.commit_url = None
                st.session_state.validation_result = None
                st.rerun()
            except GenerationError as e:
                st.session_state.gen_error = str(e)
                with st.expander("Raw response from Claude"):
                    st.code(e.raw_response)

    # GitHub push
    if push_clicked:
        github_token = _get_secret("GITHUB_TOKEN")
        if not github_token:
            st.error("GITHUB_TOKEN must be configured in secrets to push to GitHub.")
        elif not repo_override:
            st.error("Repository name is required to push to GitHub.")
        else:
            files = _build_files(st.session_state.outputs, mode)
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
