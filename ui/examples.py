"""Curated example prompts for the sidebar examples library.

All prompts here are sourced from the qa_runner.py TEST_CASES list and
are demonstrably safe (every one of them passes the 130/133 static QA
baseline as of 2026-05-03). Editing this file does not change generator
behaviour; the prompts are pure UI strings that prefill the textarea.

Categories are limited to seven so the sidebar expander stays scannable.
Each entry: (label, prompt) where label is a short human title and
prompt is the exact user-input text that gets written to
session_state.user_input_area on click.
"""

from __future__ import annotations

# Twelve prompts across six categories. Reorder freely; the only contract
# is that EXAMPLES is a list of (category, label, prompt) tuples.
EXAMPLES: list[tuple[str, str, str]] = [
    # Groups & rules
    ("Groups",         "Engineering group",
     "Create a group called Engineering"),
    ("Groups",         "Auto-assign by department",
     "Create a rule that adds users with department=Engineering to the Engineering group"),

    # Apps
    ("SAML SSO",       "Salesforce SAML",
     "Create a SAML 2.0 app for Salesforce"),
    ("SAML SSO",       "Workday + SCIM",
     "Create a SAML app called HR Portal for Workday with SCIM provisioning"),
    ("OAuth / OIDC",   "Internal dashboard",
     "Create an OAuth 2.0 app for our internal dashboard"),

    # Event hooks
    ("Event hooks",    "Mutual exclusivity",
     "When a user is added to the Tableau Creator group, remove them from Tableau Viewer and Tableau Explorer"),
    ("Event hooks",    "User deactivation",
     "Create an event hook that fires when a user is deactivated"),
    ("Event hooks",    "New user notification",
     "Set up an event hook to call an endpoint when a new user is created in Okta"),

    # Lambda / AWS
    ("AWS Lambda",     "Offboarding alert",
     "Build a Lambda that fires when a user is added to the Offboarding group and sends an SNS alert"),
    ("AWS Lambda",     "Scheduled group sync",
     "Create a daily scheduled Lambda that runs at 9 AM UTC to sync Okta groups"),

    # GCP
    ("GCP",            "Pub/Sub fanout",
     "Create a Pub/Sub topic called orders that fans out to two Cloud Functions"),
    ("GCP",            "Bucket with versioning",
     "Create a Cloud Storage bucket called document-uploads with versioning enabled"),
]


def render_examples_library() -> None:
    """Render a collapsed sidebar expander listing the curated prompts.

    Click handler writes the prompt to session_state.user_input_area and
    reruns, mirroring the existing starter-chip pattern in
    render_hero_starters. Resets in-flight intent + outputs so the
    funnel restarts cleanly.

    Streamlit is imported lazily to keep this module load side-effect
    free (matches the ui/css.py hardening pattern).
    """
    try:
        import streamlit as st
    except Exception:
        return

    with st.sidebar.expander("Example prompts", expanded=False):
        st.caption(f"{len(EXAMPLES)} curated prompts. Click to load.")
        last_category: str | None = None
        for i, (category, label, prompt) in enumerate(EXAMPLES):
            if category != last_category:
                st.markdown(f"**{category}**")
                last_category = category
            if st.button(label, key=f"ex_{i}", help=prompt, use_container_width=True):
                st.session_state["user_input_area"] = prompt
                # Reset funnel state so the example starts clean.
                st.session_state["intent"] = None
                st.session_state["outputs"] = None
                st.session_state["parse_error"] = None
                st.session_state["validation_result"] = None
                st.session_state["commit_url"] = None
                # Phase 8B versioning: clear history when loading an example.
                st.session_state["b_output_history"] = []
                st.session_state["b_active_version"] = 0
                st.rerun()
