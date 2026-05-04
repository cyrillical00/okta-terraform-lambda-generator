"""Account / help / pricing dialogs for Phase 8B B.3.

Three @st.dialog modals reachable from the sidebar:

  show_account_dialog(email)  — signed-in identity, role, today's quota
                                usage, session totals, recent audit
                                entries, link to the pricing dialog.
  show_help_dialog()          — how-it-works, provider docs links,
                                support email, examples-library pointer.
  show_pricing_dialog()       — placeholder marketing content (free vs
                                paid tier, contact-sales). No billing
                                logic; this exists so the surface is
                                ready when pricing is decided.

All three lazy-import streamlit and the data modules (audit/cost/roles)
to keep this file's import side-effect free, matching the ui/css.py +
ui/onboarding.py hardening pattern. Callers should already have
configure()'d those modules at app startup.
"""

from __future__ import annotations

_SUPPORT_EMAIL = "cyrillical@gmail.com"
_DOCS_LINKS = [
    ("Okta provider (v4.x)",   "https://registry.terraform.io/providers/okta/okta/latest/docs"),
    ("AWS provider",           "https://registry.terraform.io/providers/hashicorp/aws/latest/docs"),
    ("Google Cloud provider",  "https://registry.terraform.io/providers/hashicorp/google/latest/docs"),
    ("AWS Lambda Python API",  "https://docs.aws.amazon.com/lambda/latest/dg/python-handler.html"),
    ("GCP Cloud Functions",    "https://cloud.google.com/functions/docs/writing"),
]


def show_account_dialog(email: str) -> None:
    """Render the account modal. Call inside a button handler so the
    dialog appears in response to a click. Best-effort on every data
    pull — a failed audit/cost/roles read shows a placeholder caption
    instead of breaking the modal."""
    try:
        import streamlit as st
    except Exception:
        return
    if not hasattr(st, "dialog"):
        return

    @st.dialog("Account")
    def _show() -> None:
        st.markdown(f"**Signed in as** `{email}`")

        try:
            import roles as _roles
            role = _roles.get_role(email)
            cap = _roles.daily_quota_usd(email)
        except Exception:
            role, cap = "viewer", 0.0
        try:
            import cost as _cost
            spent = _cost.today_usd(email)
            session = _cost.total_session(email)
        except Exception:
            spent, session = 0.0, {}

        col_role, col_spend = st.columns(2)
        with col_role:
            st.caption("Role")
            st.markdown(f"**{role}**")
        with col_spend:
            st.caption("Today (UTC)")
            cap_str = "no cap" if not cap else f"of ${cap:.2f}"
            st.markdown(f"**${spent:.4f}** {cap_str}")
            if cap:
                pct = min(1.0, spent / cap) if cap else 0.0
                st.progress(pct)

        st.divider()
        st.caption("This session")
        if session:
            calls = int(session.get("calls", 0) or 0)
            sess_usd = float(session.get("usd", 0.0) or 0.0)
            inp = int(session.get("input", 0) or 0)
            out = int(session.get("output", 0) or 0)
            st.markdown(
                f"`{calls}` API call(s) · "
                f"`{inp:,}` input + `{out:,}` output tokens · "
                f"**${sess_usd:.4f}**"
            )
        else:
            st.caption("No API calls this session yet.")

        st.divider()
        st.caption("Recent activity")
        try:
            import audit as _audit
            entries = _audit.recent(email, limit=5)
        except Exception:
            entries = []
        if not entries:
            st.caption("No activity logged yet.")
        else:
            for e in entries:
                ts = (e.get("timestamp_utc") or "")[:19].replace("T", " ")
                action = e.get("action", "")
                rt = e.get("resource_type", "")
                ec = float(e.get("cost_estimate_usd") or 0.0)
                meta = f"`{action}`"
                if rt:
                    meta += f" · `{rt}`"
                if ec > 0:
                    meta += f" · ${ec:.4f}"
                st.markdown(f"{meta}  ·  <span class='tf-sidebar-timestamp'>{ts} UTC</span>", unsafe_allow_html=True)

        st.divider()
        col_pricing, col_close = st.columns([1, 1])
        with col_pricing:
            if st.button("View pricing", key="account_view_pricing", use_container_width=True):
                st.session_state["show_pricing_dialog"] = True
                st.session_state["show_account_dialog"] = False
                st.rerun()
        with col_close:
            if st.button("Close", key="account_close", use_container_width=True):
                st.session_state["show_account_dialog"] = False
                st.rerun()

    _show()


def show_help_dialog() -> None:
    """Render the in-app help modal. Static content; safe to call on
    every Streamlit run as long as it's gated by a button click."""
    try:
        import streamlit as st
    except Exception:
        return
    if not hasattr(st, "dialog"):
        return

    @st.dialog("Help")
    def _show() -> None:
        st.markdown("### How it works")
        st.markdown(
            "1. **Pick resources.** Tick the Okta / AWS / GCP rows above the "
            "prompt box, or leave them blank and let Claude infer.\n"
            "2. **Describe the operation.** Plain English. Try the starter "
            "chips on the empty state or open Example prompts in the sidebar.\n"
            "3. **Parse → confirm → generate.** Claude surfaces ambiguities "
            "for you to answer before generation. Refinement runs three "
            "validate-and-fix passes automatically.\n"
            "4. **Self-check.** Independent reviewer flags issues. Click "
            "Fix Issues to auto-patch. Use the version switcher above the "
            "code panels to flip between the last three generations.\n"
            "5. **Push.** Commits the generated files to the repo configured "
            "in the sidebar. Each prompt gets a stable filename derived from "
            "its intent (e.g. `terraform/hr_portal.tf`)."
        )
        st.divider()
        st.markdown("### Provider documentation")
        for label, url in _DOCS_LINKS:
            st.markdown(f"- [{label}]({url})")
        st.divider()
        st.markdown("### Keyboard shortcuts")
        st.markdown(
            "- `Cmd/Ctrl + Enter`  — Parse Intent\n"
            "- `Cmd/Ctrl + Shift + G`  — Generate (intent form)\n"
            "- `Cmd/Ctrl + Shift + P`  — Push to GitHub"
        )
        st.divider()
        st.markdown(f"### Support\nQuestions or bugs: `{_SUPPORT_EMAIL}`")
        if st.button("Close", key="help_close", use_container_width=True):
            st.session_state["show_help_dialog"] = False
            st.rerun()

    _show()


def show_pricing_dialog() -> None:
    """Render the pricing placeholder modal. No billing logic; this is
    marketing-style placeholder content so the surface exists when
    pricing is decided. Reachable from the account modal."""
    try:
        import streamlit as st
    except Exception:
        return
    if not hasattr(st, "dialog"):
        return

    @st.dialog("Pricing")
    def _show() -> None:
        st.caption("Placeholder pricing surface. Final pricing TBD.")

        col_free, col_team = st.columns(2)
        with col_free:
            st.markdown("### Free")
            st.markdown(
                "- 1 user\n"
                "- 50 generations / month\n"
                "- Push to your own GitHub repos\n"
                "- Community support"
            )
        with col_team:
            st.markdown("### Team")
            st.markdown(
                "- Up to 10 users\n"
                "- 1,000 generations / month\n"
                "- Push to org repos\n"
                "- Audit log retention\n"
                "- Email support"
            )

        st.divider()
        st.markdown("### Enterprise")
        st.markdown(
            "Self-hosted deployment, SAML SSO, SCIM provisioning, "
            "customer-managed encryption keys, EU data residency, "
            "SOC 2 Type 2, and a signed DPA. "
            f"Contact `{_SUPPORT_EMAIL}` to scope a pilot."
        )
        if st.button("Close", key="pricing_close", use_container_width=True):
            st.session_state["show_pricing_dialog"] = False
            st.rerun()

    _show()


def render_sidebar_links(email: str) -> None:
    """Render Account / Help / Pricing buttons in the sidebar. Each click
    flips a session-state flag; the dialogs themselves render once per
    run from app.py based on those flags. Routing lives in the caller so
    this module stays one-direction (data → UI, no callbacks)."""
    try:
        import streamlit as st
    except Exception:
        return
    col_a, col_h = st.sidebar.columns(2)
    with col_a:
        if st.button("Account", key="sb_account_btn", use_container_width=True):
            st.session_state["show_account_dialog"] = True
            st.rerun()
    with col_h:
        if st.button("Help", key="sb_help_btn", use_container_width=True):
            st.session_state["show_help_dialog"] = True
            st.rerun()


def render_dialogs(email: str) -> None:
    """Render whichever dialog is currently flagged in session state.
    Intended to be called once per app run, after the sidebar buttons
    have had a chance to flip the flags. Mutually exclusive: account →
    pricing handoff is the only chained transition."""
    try:
        import streamlit as st
    except Exception:
        return
    if st.session_state.get("show_pricing_dialog"):
        show_pricing_dialog()
        return
    if st.session_state.get("show_account_dialog"):
        show_account_dialog(email)
        return
    if st.session_state.get("show_help_dialog"):
        show_help_dialog()
        return
