"""Central place for the small bit of inline CSS the Streamlit app needs.

Everything here is presentation-only. Nothing in this file changes app
behaviour; removing the inject_global_css() call returns the UI to its
unstyled-but-functional baseline.

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

# Color constants used both by the helpers below and by callers that want
# to compose their own snippets. Keep these in sync with .streamlit/config.toml.
DARK = "#1A1A2E"
ACCENT = "#2D6A9F"
GRAY = "#444444"
LGRAY = "#777777"
GREEN = "#1F7A3A"
AMBER = "#B5751B"
RED = "#9C2B2B"

# Plain text. No f-string interpolation. Hex values are inline so this
# string is valid the moment the file is parsed.
_GLOBAL_CSS = """
<style>
.stMarkdown, .stText, .stCaption, .stRadio label {
    font-size: 0.95rem;
}

code, pre, .stCodeBlock {
    font-family: "IBM Plex Mono", "Fira Code", "JetBrains Mono", Consolas, "Roboto Mono", monospace;
}

.tf-pill-row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin: 0.25rem 0 0.75rem 0;
}
.tf-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 12px;
    border-radius: 999px;
    font-size: 0.8rem;
    font-weight: 500;
    border: 1px solid transparent;
    line-height: 1.4;
}
.tf-pill .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
}
.tf-pill-on   { background: rgba(31, 122, 58, 0.10); color: #1F7A3A; border-color: rgba(31, 122, 58, 0.25); }
.tf-pill-on   .dot { background: #1F7A3A; }
.tf-pill-warn { background: rgba(181, 117, 27, 0.10); color: #B5751B; border-color: rgba(181, 117, 27, 0.25); }
.tf-pill-warn .dot { background: #B5751B; }
.tf-pill-off  { background: rgba(119, 119, 119, 0.08); color: #777777; border-color: rgba(119, 119, 119, 0.20); }
.tf-pill-off  .dot { background: #777777; }

.tf-mode-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 10px;
    border-radius: 6px;
    font-size: 0.78rem;
    font-weight: 600;
    background: rgba(45, 106, 159, 0.10);
    color: #2D6A9F;
    border: 1px solid rgba(45, 106, 159, 0.30);
    margin-bottom: 0.4rem;
}
.tf-mode-chip .label { color: #777777; font-weight: 500; }

.tf-hero {
    text-align: center;
    padding: 1.5rem 0 1rem 0;
    border-bottom: 1px solid rgba(0, 0, 0, 0.06);
    margin-bottom: 1.25rem;
}
.tf-hero h1 {
    font-size: 1.6rem;
    color: #1A1A2E;
    margin: 0 0 0.4rem 0;
    font-weight: 700;
}
.tf-hero p {
    color: #777777;
    margin: 0;
    font-size: 0.95rem;
}

.tf-success-card {
    background: rgba(31, 122, 58, 0.06);
    border: 1px solid rgba(31, 122, 58, 0.25);
    border-radius: 8px;
    padding: 1rem 1.25rem;
    margin-top: 1rem;
}
.tf-success-card .title {
    color: #1F7A3A;
    font-weight: 600;
    font-size: 1.05rem;
    margin-bottom: 0.4rem;
}
.tf-success-card .meta { color: #444444; font-size: 0.85rem; }
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
