"""First-time-user guided tour for the TF Tool.

Renders a 5-step modal walking new users through the funnel:
  1. Welcome     — what the tool does
  2. Resources   — pick checkboxes for the resource types
  3. Describe    — write a plain-English prompt
  4. Generate    — parse + 3-pass refinement
  5. Validate    — self-check + push to GitHub

Persistence: the "I've seen this" flag lives in user_prefs (GitHub-backed
JSON keyed on the signed-in email-hash). A per-session shadow flag in
session_state prevents the dialog from re-rendering inside one session
even before the GitHub write lands. Either flag set means "don't show".

The dialog skill is a Streamlit 1.36+ feature. We import it lazily and
no-op cleanly if it isn't available so this module can never break the
page render path.
"""

from __future__ import annotations

_PREF_KEY = "onboarding_seen"
_SS_DISMISSED = "onboarding_dismissed_this_session"
_SS_STEP = "onboarding_step"

_STEPS: list[tuple[str, str]] = [
    (
        "Welcome",
        "This tool turns plain-English descriptions into deployable Terraform "
        "for Okta, AWS Lambda, and Google Cloud. One prompt → validated HCL "
        "+ Python → a GitHub commit. Five quick steps.",
    ),
    (
        "1. Pick your resources (optional)",
        "Above the prompt box you'll see Okta / AWS / GCP checkbox rows. "
        "Tick the resource types you want — or leave them all unchecked and "
        "let Claude infer from your prompt. The mode chip on the right "
        "updates as you tick.",
    ),
    (
        "2. Describe the operation",
        "Type what you want in plain English. Try the starter chips on the "
        "empty state, or open Example prompts in the sidebar for 12 curated "
        "starters. Click Parse Intent when you're ready.",
    ),
    (
        "3. Confirm & generate",
        "Claude will surface any ambiguities for you to answer, then generate "
        "+ refine the output across three passes. You'll see a live progress "
        "bar showing each pass.",
    ),
    (
        "4. Validate, then push",
        "Run Self-Check for an independent review of the generated code. "
        "Click Fix Issues to auto-patch flagged problems. When you're happy, "
        "Push to GitHub commits the files to the repo configured in the "
        "sidebar.",
    ),
]

TOTAL_STEPS = len(_STEPS)


def should_show(email: str) -> bool:
    """Return True if the tour should render for this user right now."""
    try:
        import streamlit as st
    except Exception:
        return False
    if st.session_state.get(_SS_DISMISSED):
        return False
    if not hasattr(st, "dialog"):
        return False
    try:
        import user_prefs as _up
        prefs = _up.load(email)
    except Exception:
        prefs = {}
    return not prefs.get(_PREF_KEY)


def render(email: str) -> None:
    """Render the tour dialog if the user hasn't seen it yet. Best-effort:
    any failure marks the tour dismissed for the session so we don't spam
    a broken modal on every rerun."""
    try:
        import streamlit as st
    except Exception:
        return
    if not should_show(email):
        return
    try:
        _render_dialog(email)
    except Exception:
        st.session_state[_SS_DISMISSED] = True


def _mark_seen(email: str) -> None:
    try:
        import streamlit as st
        st.session_state[_SS_DISMISSED] = True
    except Exception:
        pass
    try:
        import user_prefs as _up
        _up.update(email, **{_PREF_KEY: True})
    except Exception:
        pass


def _render_dialog(email: str) -> None:
    import streamlit as st

    step = int(st.session_state.get(_SS_STEP, 0))
    if step < 0:
        step = 0
    if step >= TOTAL_STEPS:
        _mark_seen(email)
        return

    title, body = _STEPS[step]

    @st.dialog(f"Quick tour ({step + 1} of {TOTAL_STEPS})")
    def _show() -> None:
        st.markdown(f"### {title}")
        st.markdown(body)
        st.progress((step + 1) / TOTAL_STEPS)

        col_skip, _, col_back, col_next = st.columns([1.2, 1.6, 1, 1.4])
        with col_skip:
            if st.button("Skip tour", key=f"tour_skip_{step}", use_container_width=True):
                _mark_seen(email)
                st.rerun()
        with col_back:
            back_disabled = step == 0
            if st.button("Back", key=f"tour_back_{step}", disabled=back_disabled, use_container_width=True):
                st.session_state[_SS_STEP] = max(0, step - 1)
                st.rerun()
        with col_next:
            is_last = step == TOTAL_STEPS - 1
            label = "Get started" if is_last else "Next"
            if st.button(label, key=f"tour_next_{step}", type="primary", use_container_width=True):
                if is_last:
                    _mark_seen(email)
                else:
                    st.session_state[_SS_STEP] = step + 1
                st.rerun()

    _show()
