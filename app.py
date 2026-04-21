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
from gh_push.push import push_to_github, build_commit_message
from ui.components import render_intent_card, render_code_panels, render_action_buttons


def _get_secret(key: str) -> str:
    return st.secrets.get(key) or os.getenv(key, "")


def _init_session_state():
    defaults = {
        "intent": None,
        "outputs": None,
        "parse_error": None,
        "gen_error": None,
        "commit_url": None,
        "generation_triggered": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _get_client() -> anthropic.Anthropic:
    api_key = _get_secret("ANTHROPIC_API_KEY")
    if not api_key:
        st.error("ANTHROPIC_API_KEY is not configured. Add it to .streamlit/secrets.toml or set it as an environment variable.")
        st.stop()
    return anthropic.Anthropic(api_key=api_key)


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
    client = _get_client()
    with st.spinner("Parsing intent..."):
        try:
            intent = parse_intent(user_input.strip(), client)
            errors = validate_intent(intent)
            if errors:
                st.session_state.parse_error = "Validation errors: " + "; ".join(errors)
            else:
                st.session_state.intent = intent
        except ValueError as e:
            st.session_state.parse_error = str(e)

if st.session_state.parse_error:
    st.error(st.session_state.parse_error)

# Stage 2 — Confirmation card
if st.session_state.intent and st.session_state.outputs is None:
    confirmed = render_intent_card(st.session_state.intent)
    if confirmed is not None:
        st.session_state.intent = confirmed
        st.session_state.generation_triggered = True

# Stage 3 — Generation
if st.session_state.generation_triggered:
    st.session_state.generation_triggered = False
    st.session_state.gen_error = None
    client = _get_client()
    with st.spinner("Generating Terraform HCL and Lambda Python..."):
        try:
            outputs = generate_all(st.session_state.intent, "", client)
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
    render_code_panels(st.session_state.outputs)

    push_clicked, regenerate_clicked, extra_instructions = render_action_buttons(st.session_state.outputs)

    # Regenerate
    if regenerate_clicked:
        st.session_state.gen_error = None
        client = _get_client()
        with st.spinner("Regenerating..."):
            try:
                outputs = generate_all(st.session_state.intent, extra_instructions, client)
                syntax_errors = validate_lambda_python(outputs["lambda_python"])
                if syntax_errors:
                    st.warning(f"Lambda syntax warning: {'; '.join(syntax_errors)}")
                st.session_state.outputs = outputs
                st.session_state.commit_url = None
                st.rerun()
            except GenerationError as e:
                st.session_state.gen_error = str(e)
                with st.expander("Raw response from Claude"):
                    st.code(e.raw_response)

    # GitHub push
    if push_clicked:
        github_token = _get_secret("GITHUB_TOKEN")
        github_repo = _get_secret("GITHUB_REPO")
        if not github_token or not github_repo:
            st.error("GITHUB_TOKEN and GITHUB_REPO must be configured in secrets to push to GitHub.")
        else:
            files = {
                "terraform/okta.tf": st.session_state.outputs["terraform_okta_hcl"],
                "terraform/lambda.tf": st.session_state.outputs["terraform_lambda_hcl"],
                "lambda/lambda_function.py": st.session_state.outputs["lambda_python"],
                "lambda/requirements.txt": st.session_state.outputs.get("lambda_requirements", ""),
            }
            commit_message = build_commit_message(st.session_state.intent)
            with st.spinner("Pushing to GitHub..."):
                try:
                    commit_url = push_to_github(files, github_repo, github_token, commit_message)
                    st.session_state.commit_url = commit_url
                except RuntimeError as e:
                    st.error(str(e))
                except Exception as e:
                    st.error(f"GitHub push failed: {e}")

# Stage 5 — Commit URL
if st.session_state.commit_url:
    st.success("Successfully pushed to GitHub!")
    st.link_button("View commit", st.session_state.commit_url)
