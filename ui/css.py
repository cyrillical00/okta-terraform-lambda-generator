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
@import url('https://fonts.googleapis.com/icon?family=Material+Icons|Material+Icons+Outlined|Material+Icons+Round');
@import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined&family=Material+Symbols+Rounded&display=swap');

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

/* === LIGHT THEME OVERRIDES (Phase 8B B.3 polish) ===
   Toggled via document.documentElement[data-theme="light"], set by
   inject_theme() based on the user_prefs theme value. The dark tokens
   above are the default; everything below flips background, surface,
   and text values to a light palette while keeping the ACCENT blue
   (per CLAUDE.md design constants) and the same Plex Mono typography.
   Color choices target WCAG AA contrast for body text on bg
   (#1A1A2E on #FAFAFA gives roughly 14:1, well above 4.5:1). */
[data-theme="light"] {
  --bg: #FAFAFA;
  --surface: #FFFFFF;
  --surface-2: #F0F2F5;
  --border: #D8DEE5;
  --text: #1A1A2E;
  --text-muted: #5A6470;
  --accent: #2D6A9F;
  --accent-hover: #1F4F7A;
  --accent-grad: linear-gradient(135deg, #2D6A9F 0%, #6A4FB5 100%);
  --success: #2E7D32;
  --warn: #C66900;
  --error: #C62828;
}

/* Pill / chip border tints depend on success / warn rgba values that
   were hard-coded for the dark theme. Re-tune them so the borders
   stay visible on white surfaces in light mode. */
[data-theme="light"] .tf-pill-on {
  border-color: rgba(46, 125, 50, 0.55);
}
[data-theme="light"] .tf-pill-on .dot {
  box-shadow: 0 0 6px rgba(46, 125, 50, 0.4);
}
[data-theme="light"] .tf-pill-warn {
  border-color: rgba(198, 105, 0, 0.55);
}
[data-theme="light"] .tf-pill-warn .dot {
  box-shadow: 0 0 6px rgba(198, 105, 0, 0.4);
}

/* Code-block keyword color (the inline `code` tag) needs slightly more
   contrast against the surface-2 background in light mode. */
[data-theme="light"] code {
  color: #1F4F7A;
}

/* Primary button text: the dark theme uses var(--bg) on var(--accent),
   which works because bg is near-black. In light mode bg is near-white,
   so a primary button would render white-on-blue with poor legibility.
   Force white text on the accent button for AA-compliant contrast. */
[data-theme="light"] .stButton > button[kind="primary"],
[data-theme="light"] button[kind="primary"] {
  color: #FFFFFF !important;
}
[data-theme="light"] .stButton > button[kind="primary"]:hover {
  color: #FFFFFF !important;
}

/* === RESET / GLOBAL ===
   The font reset is intentionally narrowed (no naked `span`) so Material
   Icons / Symbols spans are not stomped. Container-level selectors carry
   the font down to text descendants without forcing it on icon glyphs. */
html, body, [data-testid="stAppViewContainer"], [data-testid="stMain"], .main, .block-container,
.stMarkdown, .stText, .stCaption, p, div, label, button, input, textarea, select,
[data-baseweb], [data-testid="stMarkdownContainer"] {
  font-family: var(--font-mono) !important;
  color: var(--text);
}

/* Restore the icon font for every Streamlit icon family. Streamlit 1.56
   uses Material Symbols Rounded for stStatus chevrons, expander arrows,
   sidebar collapse handles, info/error/success badges, etc. The icon
   name is the element's text content (a font ligature); without the
   right font-family the ligature renders as the literal word
   ("arrow_right", "expand_more", "check_circle"). The selector list is
   intentionally wide because Streamlit emits at least four naming
   conventions across versions. */
.material-icons, .material-icons-outlined, .material-icons-round,
.material-symbols-outlined, .material-symbols-rounded, .material-symbols-sharp,
[data-testid="stIconMaterial"], [data-testid="stIcon"],
[data-testid="stExpanderToggleIcon"],
i[class*="material-icons"], i[class*="material-symbols"],
span[class*="material-icons"], span[class*="material-symbols"],
span[class*="icon"] [class*="material"], span[class*="Icon"] [class*="material"] {
  font-family: 'Material Symbols Rounded', 'Material Symbols Outlined',
               'Material Icons Round', 'Material Icons Outlined',
               'Material Icons', sans-serif !important;
  font-feature-settings: 'liga' !important;
  font-weight: normal !important;
  font-style: normal !important;
  text-transform: none !important;
  letter-spacing: normal !important;
  word-wrap: normal !important;
  white-space: nowrap !important;
  direction: ltr !important;
  -webkit-font-feature-settings: 'liga' !important;
  -webkit-font-smoothing: antialiased;
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

/* Hide Streamlit's default header chrome (deploy button, status widget,
   hamburger menu) without breaking the collapsed-sidebar expand button.

   In Streamlit 1.55+, the reopen control is `stExpandSidebarButton`
   sitting two levels inside `stToolbar` (the only direct child of
   `stHeader`). The previous `> *` rule nuked the toolbar wrapper and
   took the expand button down with it, leaving collapsed sidebars
   permanently hidden. The earlier patch matched legacy testids
   (stSidebarCollapsedControl / collapsedControl) that no longer exist.

   This block instead hides the specific right-side chrome we don't
   want, and pins the expand button to a fixed top-left position so it
   has a real clickable area despite the `height: 0` clip on stHeader. */
[data-testid="stHeader"] {
  background: transparent !important;
  height: 0 !important;
}
[data-testid="stHeader"] [data-testid="stHeaderActionElements"],
[data-testid="stHeader"] [data-testid="stStatusWidget"],
[data-testid="stHeader"] [data-testid="stMainMenu"] {
  display: none !important;
}
[data-testid="stExpandSidebarButton"] {
  position: fixed !important;
  top: 0.5rem !important;
  left: 0.75rem !important;
  z-index: 999 !important;
  display: inline-flex !important;
  visibility: visible !important;
  pointer-events: auto !important;
}
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
/* Sidebar expanders need a touch of margin so a help-icon tooltip
   from the previous widget can't overlap the expander's chevron arrow,
   and so the new sidebar groups (Connections, Activity, Admin) don't
   visually mash together. Non-sidebar expanders keep their natural
   spacing. */
[data-testid="stSidebar"] [data-testid="stExpander"] {
  margin-top: 8px !important;
  margin-bottom: 6px !important;
}

/* === TOOLTIP / HELP-ICON POPOVER ===
   Streamlit's (?) help icon renders a tooltip via baseweb popover. In the
   narrow sidebar column, the popover can stack underneath an adjacent
   element if its z-index isn't lifted. Pin it above sidebar content so
   the tooltip + arrow are always on top. */
[data-baseweb="tooltip"], [data-baseweb="popover"] {
  z-index: 1000 !important;
}
[data-testid="stTooltipIcon"], [data-testid="stTooltipHoverTarget"] {
  z-index: 5 !important;
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

/* === MOBILE BREAKPOINT (Phase 8B B.3 polish) ===
   Single breakpoint at 768px. Streamlit auto-collapses the sidebar
   below ~640px on its own; the rules below tighten the layout for
   the 640-768 band and reflow the multi-column code panels into a
   stacked single column so generated Terraform / Lambda code is
   readable on phone-width screens. Keep this block last so the
   media query overrides win against the general styles above. */
@media (max-width: 768px) {
  .main .block-container {
    padding-left: 0.75rem !important;
    padding-right: 0.75rem !important;
    padding-top: 1rem !important;
  }

  /* Hero font shrink for narrow viewports */
  .tf-hero {
    padding: 1rem 0 0.75rem 0;
  }
  .tf-hero h1 {
    font-size: 22px !important;
    line-height: 1.25;
  }
  .tf-hero p {
    font-size: 13px;
  }

  /* Status pill row: allow wrap (already flex-wrap above, restated for
     clarity) and shrink the per-pill padding so 3-4 pills fit on a row. */
  .tf-pill-row {
    gap: 6px;
  }
  .tf-pill, .tf-mode-chip {
    padding: 3px 9px;
    font-size: 11px;
  }

  /* Tabs: allow the tab bar to wrap when there are too many tabs to fit */
  .stTabs [role="tablist"] {
    flex-wrap: wrap;
  }
  .stTabs [role="tab"] {
    padding: 8px 12px !important;
    font-size: 13px !important;
  }

  /* Code panels: app.py:render_code_panels uses st.columns(2) for the
     Terraform / Lambda split. st.columns has no native breakpoint hook,
     so force the columns to stack vertically by overriding the flex
     basis on Streamlit's horizontal block. */
  [data-testid="stHorizontalBlock"] {
    flex-direction: column !important;
  }
  [data-testid="stHorizontalBlock"] > [data-testid="stColumn"],
  [data-testid="stHorizontalBlock"] > [data-testid="column"] {
    flex: 1 1 100% !important;
    width: 100% !important;
    min-width: 100% !important;
  }

  /* Code blocks: shrink the font slightly so long lines wrap less often */
  [data-testid="stCodeBlock"], [data-testid="stCodeBlock"] pre,
  [data-testid="stCodeBlock"] code {
    font-size: 12px !important;
  }

  /* Sidebar: when expanded on a narrow screen, occupy more of the
     viewport width so its contents are not cropped. Streamlit's own
     auto-collapse keeps this from being an issue below ~640px. */
  [data-testid="stSidebar"] {
    width: 85vw !important;
    min-width: 85vw !important;
  }

  /* Intent card / mode chip: make sure they wrap rather than overflow */
  .tf-mode-chip {
    margin-bottom: 0.4rem;
  }
}
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


def inject_theme(theme: str) -> None:
    """Set the data-theme attribute on <html> so the CSS variable
    overrides for light mode (defined under [data-theme="light"]) take
    effect. Accepts "dark", "light", or "auto"; "auto" defers to the
    user's OS-level prefers-color-scheme via a small media query check.

    Streamlit does not expose a body-attribute API, so we ship a tiny
    inline script that runs on every render and sets the attribute on
    documentElement. The script is idempotent (it can run many times per
    session without compounding effects) and runs inside the iframe-free
    main document, so the data-theme selector flips immediately.

    Best-effort: any rendering failure is swallowed so a Streamlit
    runtime quirk cannot break the rest of the page.
    """
    try:
        import streamlit as st
        choice = (theme or "dark").strip().lower()
        if choice not in ("dark", "light", "auto"):
            choice = "dark"
        if choice == "auto":
            script = (
                "<script>(function(){"
                "var prefers=window.matchMedia&&"
                "window.matchMedia('(prefers-color-scheme: light)').matches;"
                "document.documentElement.setAttribute("
                "'data-theme', prefers?'light':'dark');"
                "})();</script>"
            )
        else:
            script = (
                f"<script>document.documentElement.setAttribute("
                f"'data-theme','{choice}');</script>"
            )
        st.markdown(script, unsafe_allow_html=True)
    except Exception:
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


# Phase 8B B.2: keyboard shortcuts. Implemented with raw JS rather than the
# streamlit-shortcuts package so we don't add a new wheel that might break
# the Streamlit Cloud build (we got bitten by streamlit==1.57.0 once; pinning
# transitive deps is a pain). Listener walks the DOM on each keypress and
# synthesizes a click on the first matching button — silent no-op when the
# target isn't currently rendered (e.g. Push when no outputs exist), so the
# shortcuts never raise visible errors. Bound to the document so they fire
# regardless of focus.
_SHORTCUTS_JS = """
<script>
(function() {
  if (window.__tfShortcutsBound) return;
  window.__tfShortcutsBound = true;
  function findButton(matchText) {
    const m = matchText.toLowerCase();
    const btns = document.querySelectorAll('button');
    for (const b of btns) {
      const t = (b.innerText || b.textContent || '').trim().toLowerCase();
      if (t === m) return b;
    }
    return null;
  }
  function clickIf(matchText) {
    const b = findButton(matchText);
    if (b) { b.click(); return true; }
    return false;
  }
  document.addEventListener('keydown', function(e) {
    const mod = e.ctrlKey || e.metaKey;
    if (!mod) return;
    if (e.key === 'Enter' && !e.shiftKey) {
      if (clickIf('parse intent')) { e.preventDefault(); }
    } else if (e.shiftKey && (e.key === 'G' || e.key === 'g')) {
      if (clickIf('generate')) { e.preventDefault(); }
    } else if (e.shiftKey && (e.key === 'P' || e.key === 'p')) {
      if (clickIf('push to github')) { e.preventDefault(); }
    }
  }, true);
})();
</script>
"""


def inject_keyboard_shortcuts() -> None:
    """Inject the keyboard-shortcut listener once per Streamlit run.

    Bindings:
      Ctrl/Cmd+Enter      → Parse Intent
      Ctrl/Cmd+Shift+G    → Generate (intent form submit)
      Ctrl/Cmd+Shift+P    → Push to GitHub

    Best-effort and idempotent (the script self-guards via window flag).
    Silent no-op when the target button isn't currently rendered so the
    shortcuts never raise a visible error.
    """
    try:
        import streamlit as st
        st.markdown(_SHORTCUTS_JS, unsafe_allow_html=True)
    except Exception:
        pass
