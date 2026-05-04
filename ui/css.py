"""Central place for the inline CSS the Streamlit app needs.

Everything here is presentation-only. Nothing in this file changes app
behaviour; removing the inject_global_css() call returns the UI to its
unstyled-but-functional baseline.

Phase 9 (2026-05-03) rewrote the stylesheet for a dark-terminal aesthetic
anchored on IBM Plex Mono. Streamlit 1.56's config.toml does not honour
the newer headingFont/codeFont/baseRadius slots, so all visual tokens are
defined as CSS custom properties on :root here and applied via classes
that override Streamlit's component selectors directly.

Hardening notes:
- Module-level code runs no I/O and imports nothing beyond the stdlib at
  load time. Streamlit is imported lazily inside inject_global_css() so a
  partially-initialized streamlit (e.g. during a Cloud rebuild race)
  cannot break this module's import.
- The CSS string is plain text with no f-string interpolation. Color
  constants are still exported for callers, but the stylesheet uses raw
  hex inline. This avoids any f-string brace-escape surprises during
  module load.
- inject_global_css() and the helpers swallow any rendering exception
  with a defensive try/except so CSS issues never block the rest of the
  page from rendering.
"""

from __future__ import annotations

# Color constants for callers that compose their own snippets.
# Keep in sync with the :root tokens below and .streamlit/config.toml.
DARK = "#0A0E14"          # bg
SURFACE = "#11161D"       # cards, code blocks
SURFACE_2 = "#161C25"     # hover, active selection
BORDER = "#232A36"
TEXT = "#E6EDF3"
TEXT_MUTED = "#7A8694"
ACCENT = "#4FC3F7"
ACCENT_HOVER = "#7FD7FF"
GREEN = "#3FB950"
AMBER = "#D29922"
RED = "#F85149"
# Legacy aliases preserved so any caller that imported the old names keeps working.
GRAY = TEXT_MUTED
LGRAY = TEXT_MUTED

_GLOBAL_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap');

:root {
  --bg: #0A0E14;
  --surface: #11161D;
  --surface-2: #161C25;
  --border: #232A36;
  --text: #E6EDF3;
  --text-muted: #7A8694;
  --accent: #4FC3F7;
  --accent-hover: #7FD7FF;
  --accent-grad: linear-gradient(135deg, #4FC3F7 0%, #B388FF 100%);
  --success: #3FB950;
  --warn: #D29922;
  --error: #F85149;
  --font-mono: 'IBM Plex Mono', 'JetBrains Mono', 'Fira Code', Consolas, monospace;
  --font-sans: 'IBM Plex Sans', 'Inter', -apple-system, sans-serif;
  --fs-meta: 12px;
  --fs-body: 14px;
  --fs-h2: 18px;
  --fs-hero: 28px;
}

/* === RESET / GLOBAL === */
html, body, [data-testid="stAppViewContainer"], [data-testid="stMain"], .main, .block-container,
.stMarkdown, .stText, .stCaption, p, div, span, label, button, input, textarea, select,
[data-baseweb], [data-testid="stMarkdownContainer"] {
  font-family: var(--font-mono) !important;
  color: var(--text);
}

body, [data-testid="stAppViewContainer"], .main {
  background: var(--bg) !important;
  color: var(--text) !important;
}

.main .block-container {
  padding-top: 2rem;
  padding-bottom: 4rem;
  max-width: 1280px;
}

/* Hide Streamlit's default header (the "Manage app" / hamburger bar). */
[data-testid="stHeader"] {
  background: transparent !important;
  height: 0 !important;
}
[data-testid="stHeader"] > * { display: none !important; }
#MainMenu { visibility: hidden; }
footer { visibility: hidden; }

/* === TYPE === */
h1, h2, h3, h4, h5, h6,
[data-testid="stMarkdownContainer"] h1,
[data-testid="stMarkdownContainer"] h2,
[data-testid="stMarkdownContainer"] h3 {
  font-family: var(--font-mono) !important;
  font-weight: 600 !important;
  color: var(--text) !important;
  letter-spacing: -0.005em;
}

[data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li,
.stCaption {
  font-size: var(--fs-body);
  line-height: 1.55;
}

.stCaption, [data-testid="stCaptionContainer"] {
  color: var(--text-muted) !important;
  font-size: var(--fs-meta) !important;
  letter-spacing: 0.02em;
}

/* === SIDEBAR === */
[data-testid="stSidebar"], [data-testid="stSidebar"] > div {
  background: var(--surface) !important;
  border-right: 1px solid var(--border);
}
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] li {
  color: var(--text);
}
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] strong {
  color: var(--accent);
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  font-size: 11px;
}

/* === BUTTONS === */
.stButton > button, .stDownloadButton > button, [data-testid="stLinkButton"] a {
  background: var(--surface-2) !important;
  border: 1px solid var(--border) !important;
  color: var(--text) !important;
  font-family: var(--font-mono) !important;
  font-size: var(--fs-body) !important;
  font-weight: 500;
  border-radius: 4px !important;
  padding: 8px 16px !important;
  transition: border-color 120ms ease-out, background 120ms ease-out, color 120ms ease-out;
  box-shadow: none !important;
}
.stButton > button:hover, .stDownloadButton > button:hover, [data-testid="stLinkButton"] a:hover {
  border-color: var(--accent) !important;
  color: var(--accent) !important;
  background: var(--surface-2) !important;
}
.stButton > button:focus, .stButton > button:focus-visible {
  outline: 1px solid var(--accent) !important;
  outline-offset: 2px;
}
.stButton > button[kind="primary"], button[kind="primary"] {
  background: var(--accent) !important;
  border-color: var(--accent) !important;
  color: var(--bg) !important;
  font-weight: 600;
}
.stButton > button[kind="primary"]:hover {
  background: var(--accent-hover) !important;
  border-color: var(--accent-hover) !important;
  color: var(--bg) !important;
}

/* === TEXT INPUTS / TEXTAREAS / SELECT === */
.stTextInput input, .stTextArea textarea,
[data-baseweb="input"] input, [data-baseweb="textarea"] textarea,
[data-baseweb="select"] > div, [data-baseweb="select"] input {
  background: var(--surface) !important;
  border: 1px solid var(--border) !important;
  color: var(--text) !important;
  font-family: var(--font-mono) !important;
  font-size: var(--fs-body) !important;
  border-radius: 4px !important;
  box-shadow: none !important;
}
.stTextInput input:focus, .stTextArea textarea:focus,
[data-baseweb="input"] input:focus, [data-baseweb="textarea"] textarea:focus {
  border-color: var(--accent) !important;
  box-shadow: 0 0 0 1px var(--accent) inset !important;
  outline: none !important;
}
.stTextInput input::placeholder, .stTextArea textarea::placeholder {
  color: var(--text-muted) !important;
  opacity: 0.7;
}

/* Selectbox dropdown panel */
[data-baseweb="popover"] [role="listbox"], [data-baseweb="menu"] {
  background: var(--surface) !important;
  border: 1px solid var(--border) !important;
}
[data-baseweb="popover"] [role="option"]:hover, [data-baseweb="menu"] li:hover {
  background: var(--surface-2) !important;
}

/* === CHECKBOXES / RADIO === */
.stCheckbox label, .stRadio label, [data-baseweb="radio"] label, [data-baseweb="checkbox"] label {
  color: var(--text) !important;
  font-family: var(--font-mono) !important;
  font-size: var(--fs-body) !important;
}
[data-baseweb="checkbox"] [role="checkbox"][aria-checked="true"],
[data-baseweb="radio"] [role="radio"][aria-checked="true"] {
  background: var(--accent) !important;
  border-color: var(--accent) !important;
}

/* === EXPANDER === */
[data-testid="stExpander"], details[data-testid="stExpander"] {
  background: var(--surface) !important;
  border: 1px solid var(--border) !important;
  border-radius: 4px !important;
}
[data-testid="stExpander"] summary, [data-testid="stExpander"] [data-testid="stExpanderToggleIcon"] {
  color: var(--text) !important;
  font-family: var(--font-mono) !important;
}
[data-testid="stExpander"] summary:hover {
  background: var(--surface-2) !important;
}

/* === TABS === */
.stTabs [role="tablist"] {
  border-bottom: 1px solid var(--border) !important;
  gap: 0;
}
.stTabs [role="tab"] {
  background: transparent !important;
  color: var(--text-muted) !important;
  font-family: var(--font-mono) !important;
  font-size: var(--fs-body) !important;
  font-weight: 500;
  padding: 10px 16px !important;
  border-bottom: 2px solid transparent !important;
  border-radius: 0 !important;
}
.stTabs [role="tab"][aria-selected="true"] {
  color: var(--accent) !important;
  border-bottom-color: var(--accent) !important;
  background: transparent !important;
}

/* === CODE BLOCKS === */
[data-testid="stCodeBlock"], [data-testid="stCodeBlock"] pre, .stCodeBlock {
  background: var(--surface) !important;
  border: 1px solid var(--border) !important;
  border-radius: 6px !important;
  font-family: var(--font-mono) !important;
  font-size: 13px !important;
}
[data-testid="stCodeBlock"] code, [data-testid="stCodeBlock"] pre code {
  font-family: var(--font-mono) !important;
  font-size: 13px !important;
  color: var(--text) !important;
}
code {
  background: var(--surface-2) !important;
  color: var(--accent) !important;
  padding: 1px 6px;
  border-radius: 3px;
  font-family: var(--font-mono) !important;
}

/* === STATUS / PROGRESS / ALERTS === */
[data-testid="stStatusWidget"], [data-testid="stStatus"] {
  background: var(--surface) !important;
  border: 1px solid var(--border) !important;
  border-radius: 4px;
}
[data-testid="stProgress"] > div > div > div {
  background: var(--surface-2) !important;
}
[data-testid="stProgress"] > div > div > div > div {
  background: var(--accent) !important;
  background-image: var(--accent-grad) !important;
}
[data-testid="stNotification"] {
  background: var(--surface) !important;
  border: 1px solid var(--border) !important;
  color: var(--text) !important;
  border-radius: 4px;
}
[data-testid="stAlert"][kind="success"], [data-testid="stNotification"][kind="success"] {
  border-left: 3px solid var(--success) !important;
}
[data-testid="stAlert"][kind="warning"], [data-testid="stNotification"][kind="warning"] {
  border-left: 3px solid var(--warn) !important;
}
[data-testid="stAlert"][kind="error"], [data-testid="stNotification"][kind="error"] {
  border-left: 3px solid var(--error) !important;
}

/* === DIVIDER === */
hr, [data-testid="stDivider"] {
  border-color: var(--border) !important;
  background: var(--border) !important;
}

/* === SCROLLBAR === */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 5px; }
::-webkit-scrollbar-thumb:hover { background: var(--surface-2); }

/* === TF-PILL FAMILY (status indicators) === */
.tf-pill-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin: 0.5rem 0 1rem 0;
}
.tf-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 4px 12px;
  border-radius: 4px;
  font-size: var(--fs-meta);
  font-family: var(--font-mono);
  font-weight: 500;
  letter-spacing: 0.02em;
  background: var(--surface);
  border: 1px solid var(--border);
  color: var(--text-muted);
  text-transform: uppercase;
}
.tf-pill .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  display: inline-block;
}
.tf-pill-on {
  color: var(--success);
  border-color: rgba(63, 185, 80, 0.45);
}
.tf-pill-on .dot {
  background: var(--success);
  box-shadow: 0 0 6px rgba(63, 185, 80, 0.7);
}
.tf-pill-warn {
  color: var(--warn);
  border-color: rgba(210, 153, 34, 0.45);
}
.tf-pill-warn .dot {
  background: var(--warn);
  box-shadow: 0 0 6px rgba(210, 153, 34, 0.6);
}
.tf-pill-off {
  color: var(--text-muted);
  border-color: var(--border);
}
.tf-pill-off .dot {
  background: var(--text-muted);
}

/* === MODE CHIP === */
.tf-mode-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 4px 12px 4px 14px;
  border-radius: 4px;
  font-size: var(--fs-meta);
  font-family: var(--font-mono);
  font-weight: 500;
  background: var(--surface);
  color: var(--accent);
  border: 1px solid var(--border);
  border-left: 2px solid var(--accent);
  letter-spacing: 0.04em;
  text-transform: uppercase;
  margin-bottom: 0.6rem;
}
.tf-mode-chip .label {
  color: var(--text-muted);
  font-weight: 500;
}

/* === HERO === */
.tf-hero {
  text-align: left;
  padding: 2rem 0 1.5rem 0;
  border-bottom: 1px solid var(--border);
  margin-bottom: 1.5rem;
}
.tf-hero h1 {
  font-size: var(--fs-hero);
  font-family: var(--font-mono);
  font-weight: 600;
  color: var(--text);
  margin: 0 0 0.5rem 0;
  letter-spacing: -0.01em;
  line-height: 1.2;
}
.tf-hero h1::after {
  content: '_';
  color: var(--accent);
  margin-left: 4px;
  animation: tf-cursor-blink 1.1s steps(1) infinite;
  font-weight: 400;
}
.tf-hero p {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: var(--fs-body);
  margin: 0;
  letter-spacing: 0.01em;
}

@keyframes tf-cursor-blink {
  50% { opacity: 0; }
}

/* === SUCCESS CARD === */
.tf-success-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-left: 2px solid var(--success);
  border-radius: 4px;
  padding: 1rem 1.25rem;
  margin-top: 1rem;
}
.tf-success-card .title {
  color: var(--success);
  font-family: var(--font-mono);
  font-weight: 600;
  font-size: var(--fs-h2);
  margin-bottom: 0.4rem;
  letter-spacing: 0.01em;
}
.tf-success-card .meta {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: var(--fs-meta);
  letter-spacing: 0.02em;
}
.tf-success-card .meta b {
  color: var(--text);
  font-weight: 500;
}

/* === SIDEBAR HELPER CLASSES === */
.tf-sidebar-timestamp {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.03em;
  display: block;
}
.tf-sidebar-preview {
  color: var(--text);
  font-family: var(--font-mono);
  font-size: 13px;
  display: block;
  line-height: 1.4;
}
.tf-sidebar-role {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: var(--fs-meta);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.tf-sidebar-role b {
  color: var(--accent);
  font-weight: 600;
}

/* === OUTPUT REVEAL ANIMATION === */
@keyframes tf-fade-in {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: none; }
}
[data-testid="stCodeBlock"] {
  animation: tf-fade-in 240ms ease-out;
}

/* === VALIDATION GROUPING (Thread B D2) === */
.tf-issue-file {
  display: inline-block;
  font-family: var(--font-mono);
  font-size: var(--fs-meta);
  background: var(--surface-2);
  color: var(--accent);
  padding: 1px 6px;
  border-radius: 3px;
  margin-right: 6px;
}
.tf-issue-severity-error { color: var(--error); }
.tf-issue-severity-warn  { color: var(--warn); }
.tf-issue-severity-info  { color: var(--text-muted); }
</style>
"""


def inject_global_css() -> None:
    """Inject the global CSS once per Streamlit run. Idempotent and best-effort.

    Streamlit is imported lazily so a module-load failure here can never
    break the rest of the app (the helpers below also no-op gracefully on
    any rendering exception).
    """
    try:
        import streamlit as st  # local import keeps module load side-effect free
        st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)
    except Exception:
        # If Streamlit is mid-initialization or the runtime is unusual,
        # we'd rather lose styling than crash the page.
        pass


def pill(label: str, state: str, tooltip: str = "") -> str:
    """Return an HTML string for a status pill. State: on / warn / off.

    Use inside `st.markdown(..., unsafe_allow_html=True)`.
    Defensive: any unexpected state value falls back to 'off'.
    """
    cls_map = {"on": "tf-pill-on", "warn": "tf-pill-warn", "off": "tf-pill-off"}
    cls = cls_map.get(state, "tf-pill-off")
    safe_label = (label or "").replace("<", "&lt;").replace(">", "&gt;")
    safe_tooltip = (tooltip or "").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    title_attr = f' title="{safe_tooltip}"' if safe_tooltip else ""
    return (
        f'<span class="tf-pill {cls}"{title_attr}>'
        f'<span class="dot"></span>{safe_label}</span>'
    )


def mode_chip_html(mode: str) -> str:
    """Return HTML for the read-only mode indicator chip."""
    safe_mode = (mode or "").replace("<", "&lt;").replace(">", "&gt;")
    return f'<div class="tf-mode-chip"><span class="label">Mode</span> &middot; {safe_mode}</div>'
