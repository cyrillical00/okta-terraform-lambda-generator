"""Central place for the small bit of inline CSS the Streamlit app needs.

Everything here is presentation-only. Nothing in this file changes app
behaviour; removing the inject_global_css() call returns the UI to its
unstyled-but-functional baseline.
"""

from __future__ import annotations

import streamlit as st

DARK = "#1A1A2E"
ACCENT = "#2D6A9F"
GRAY = "#444444"
LGRAY = "#777777"
GREEN = "#1F7A3A"
AMBER = "#B5751B"
RED = "#9C2B2B"

_GLOBAL_CSS = f"""
<style>
/* Slightly larger base font for readability on demo screens. */
.stMarkdown, .stText, .stCaption, .stRadio label {{
    font-size: 0.95rem;
}}

/* Code blocks: monospace family + better contrast for HCL. */
code, pre, .stCodeBlock {{
    font-family: "IBM Plex Mono", "Fira Code", "JetBrains Mono", Consolas, "Roboto Mono", monospace;
}}

/* Custom pill row used for env-status badges. */
.tf-pill-row {{
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin: 0.25rem 0 0.75rem 0;
}}
.tf-pill {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 12px;
    border-radius: 999px;
    font-size: 0.8rem;
    font-weight: 500;
    border: 1px solid transparent;
    line-height: 1.4;
}}
.tf-pill .dot {{
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
}}
.tf-pill-on   {{ background: rgba(31, 122, 58, 0.10); color: {GREEN}; border-color: rgba(31, 122, 58, 0.25); }}
.tf-pill-on   .dot {{ background: {GREEN}; }}
.tf-pill-warn {{ background: rgba(181, 117, 27, 0.10); color: {AMBER}; border-color: rgba(181, 117, 27, 0.25); }}
.tf-pill-warn .dot {{ background: {AMBER}; }}
.tf-pill-off  {{ background: rgba(119, 119, 119, 0.08); color: {LGRAY}; border-color: rgba(119, 119, 119, 0.20); }}
.tf-pill-off  .dot {{ background: {LGRAY}; }}

/* Mode indicator chip. */
.tf-mode-chip {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 10px;
    border-radius: 6px;
    font-size: 0.78rem;
    font-weight: 600;
    background: rgba(45, 106, 159, 0.10);
    color: {ACCENT};
    border: 1px solid rgba(45, 106, 159, 0.30);
    margin-bottom: 0.4rem;
}}
.tf-mode-chip .label {{ color: {LGRAY}; font-weight: 500; }}

/* Hero block on empty state. */
.tf-hero {{
    text-align: center;
    padding: 1.5rem 0 1rem 0;
    border-bottom: 1px solid rgba(0, 0, 0, 0.06);
    margin-bottom: 1.25rem;
}}
.tf-hero h1 {{
    font-size: 1.6rem;
    color: {DARK};
    margin: 0 0 0.4rem 0;
    font-weight: 700;
}}
.tf-hero p {{
    color: {LGRAY};
    margin: 0;
    font-size: 0.95rem;
}}

/* Success card for post-commit state. */
.tf-success-card {{
    background: rgba(31, 122, 58, 0.06);
    border: 1px solid rgba(31, 122, 58, 0.25);
    border-radius: 8px;
    padding: 1rem 1.25rem;
    margin-top: 1rem;
}}
.tf-success-card .title {{
    color: {GREEN};
    font-weight: 600;
    font-size: 1.05rem;
    margin-bottom: 0.4rem;
}}
.tf-success-card .meta {{ color: {GRAY}; font-size: 0.85rem; }}
</style>
"""


def inject_global_css() -> None:
    """Inject the global CSS once per Streamlit run. Idempotent."""
    st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)


def pill(label: str, state: str, tooltip: str = "") -> str:
    """Return an HTML string for a status pill. State: on / warn / off.

    Use inside `st.markdown(..., unsafe_allow_html=True)`.
    """
    cls = {"on": "tf-pill-on", "warn": "tf-pill-warn", "off": "tf-pill-off"}.get(state, "tf-pill-off")
    title_attr = f' title="{tooltip}"' if tooltip else ""
    return f'<span class="tf-pill {cls}"{title_attr}><span class="dot"></span>{label}</span>'


def mode_chip_html(mode: str) -> str:
    """Return HTML for the read-only mode indicator chip."""
    return f'<div class="tf-mode-chip"><span class="label">Mode</span> · {mode}</div>'
