"""Cost & Usage dashboard (Phase 22a).

Renders a sidebar block showing today's spend, last 7-day trend, top 5
expensive prompts, and cache-savings call-out. Reads from the existing
surfaces only:

  - cost.today_usd / cost.total_session for current spend snapshots
  - cost daily-totals JSON (local file or GitHub-backed) for the 7-day
    history bar chart and the week-over-week trend
  - audit.recent for the top-N expensive prompts (uses cost_estimate_usd
    + redacted_input_preview on each entry)

No new dependencies. Uses Streamlit's native chart primitives
(st.bar_chart, st.metric). All reads are best-effort; on any failure the
dashboard renders the empty-state instead of raising into the UI.

Entry point: render_cost_dashboard(env_context, user_email). Callers
wrap it in an outer expander.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import cost as _cost


_LOCAL_USAGE_PATH = ".streamlit/usage_local.json"
_TOP_PROMPT_LIMIT = 5
_PREVIEW_CHARS = 80
_HISTORY_DAYS = 14  # 7 current week + 7 prior, enough for WoW trend
_AUDIT_PULL_LIMIT = 200  # cap audit fetch; older entries don't affect 7-day window


# ── data access (read-only against cost.py surface) ──────────────────────


def _email_hash(email: str) -> str:
    # Mirror cost._email_hash. Duplicated here so we don't import a
    # private symbol; the hash is the public storage key.
    import hashlib
    return hashlib.sha256((email or "anonymous").encode("utf-8")).hexdigest()[:16]


def _read_daily_totals(email: str) -> dict[str, float]:
    """Return {YYYY-MM-DD: usd} for the given actor.

    Tries GitHub first when configured, then falls back to the local
    usage file. Returns an empty dict on any failure (empty-state
    rendering downstream).
    """
    token = (getattr(_cost, "_github_token", "") or "").strip()
    repo = (getattr(_cost, "_github_repo", "") or "").strip()
    if token and repo:
        try:
            from github import Github, GithubException
            g = Github(token)
            r = g.get_repo(repo)
            path = f"_tftool/usage/{_email_hash(email)}.json"
            try:
                contents = r.get_contents(path)
                data = json.loads(base64.b64decode(contents.content).decode("utf-8"))
                if isinstance(data, dict):
                    return {k: float(v or 0.0) for k, v in data.items() if isinstance(k, str)}
            except GithubException as e:
                if getattr(e, "status", None) == 404:
                    return {}
        except Exception:
            pass
    try:
        with open(_LOCAL_USAGE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        per_user = data.get(email or "anonymous", {}) or {}
        if isinstance(per_user, dict):
            return {k: float(v or 0.0) for k, v in per_user.items() if isinstance(k, str)}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass
    return {}


def _build_window(daily: dict[str, float], days: int) -> list[tuple[str, float]]:
    """Return the last `days` UTC dates ending today, each as (label, usd).

    Days with no record show $0. Result is oldest -> newest so a bar chart
    reads left to right like a calendar.
    """
    today = datetime.now(timezone.utc).date()
    out: list[tuple[str, float]] = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        key = d.strftime("%Y-%m-%d")
        out.append((key, float(daily.get(key, 0.0) or 0.0)))
    return out


def _trend(daily: dict[str, float]) -> tuple[float, float, float | None]:
    """Compute (this_week_usd, prior_week_usd, percent_delta).

    percent_delta is None when the prior week is zero (avoid div-by-zero
    + meaningless "infinity percent" arrow). Caller renders an em-dash
    placeholder in that case via Streamlit's native metric formatting.
    """
    window = _build_window(daily, 14)
    if len(window) < 14:
        return 0.0, 0.0, None
    prior = sum(v for _, v in window[:7])
    current = sum(v for _, v in window[7:])
    if prior <= 0:
        return current, prior, None
    return current, prior, ((current - prior) / prior) * 100.0


def _recent_audit_entries(email: str, limit: int) -> list[dict]:
    try:
        import audit
        return audit.recent(email, limit=limit) or []
    except Exception:
        return []


def _top_prompts(entries: list[dict], days: int, k: int) -> list[dict]:
    """Filter audit entries to the last `days`, sort by cost desc, take k."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    scored: list[tuple[float, dict]] = []
    for e in entries:
        ts_raw = (e.get("timestamp_utc") or "").strip()
        if not ts_raw:
            continue
        try:
            # tolerate both "+00:00" and "Z" suffixes
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue
        cost_usd = float(e.get("cost_estimate_usd", 0.0) or 0.0)
        if cost_usd <= 0:
            continue
        preview = (e.get("redacted_input_preview") or "").strip()
        if not preview:
            continue
        scored.append((cost_usd, {"cost": cost_usd, "preview": preview, "ts": ts_raw}))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [item for _, item in scored[:k]]


def _cache_savings(email: str) -> float:
    """Estimate USD saved this session by cache reads.

    Each cache-read token cost $0.10/M instead of the regular $1/M input
    price; the saving is the delta (0.90/M) times cache_read tokens.
    Read straight from cost.total_session so we don't double-count
    persisted history.
    """
    sess = _cost.total_session(email) or {}
    cache_read = float(sess.get("cache_read", 0) or 0)
    if cache_read <= 0:
        return 0.0
    delta_per_million = _cost.PRICE_INPUT_PER_M - _cost.PRICE_CACHE_READ_PER_M
    return round(cache_read * delta_per_million / 1_000_000.0, 4)


def _cache_hit_rate(email: str) -> float | None:
    """Return cache-hit rate (cache_read / (cache_read + uncached input))
    as a percent in [0, 100], or None when no tokens have flowed."""
    sess = _cost.total_session(email) or {}
    cache_read = float(sess.get("cache_read", 0) or 0)
    inp = float(sess.get("input", 0) or 0)
    denom = cache_read + inp
    if denom <= 0:
        return None
    return round(100.0 * cache_read / denom, 1)


# ── render ───────────────────────────────────────────────────────────────


def render_cost_dashboard(
    env_context: dict | None,
    user_email: str,
    *,
    today_usd: float | None = None,
    daily_totals: dict[str, float] | None = None,
    audit_entries: list[dict] | None = None,
) -> None:
    """Streamlit-side renderer. Container-agnostic: writes flow into
    whichever container the caller is `with`-ing (typically a sidebar
    expander).

    The three GitHub-backed reads (today's spend, daily totals, recent
    audit) can be injected by the caller. app.py passes cached values so
    the dashboard does not re-hit GitHub on every rerun; when omitted (the
    test path and any direct caller) the dashboard fetches them itself.
    """
    import streamlit as st

    email = (user_email or "").strip() or "anonymous"

    if today_usd is None:
        try:
            today_usd = float(_cost.today_usd(email) or 0.0)
        except Exception:
            today_usd = 0.0

    session = _cost.total_session(email) or {}
    prompt_count = int(session.get("calls", 0) or 0)
    cache_hit = _cache_hit_rate(email)

    # Row 1: three metric cards.
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Today", f"${today_usd:,.2f}")
    with c2:
        st.metric("Prompts (session)", f"{prompt_count}")
    with c3:
        st.metric(
            "Cache hit rate",
            "n/a" if cache_hit is None else f"{cache_hit:.1f}%",
        )

    daily = daily_totals if daily_totals is not None else _read_daily_totals(email)
    window7 = _build_window(daily, 7)
    has_any_spend = any(v > 0 for _, v in window7)

    st.caption("Last 7 days")
    if has_any_spend:
        # st.bar_chart accepts a {label: value} dict and renders one bar
        # per key in insertion order. No new deps needed.
        chart_data = {label: round(value, 4) for label, value in window7}
        st.bar_chart(chart_data, height=160)
    else:
        st.caption("No spend recorded in the last 7 days.")

    # Week-over-week trend with arrow + percentage. Streamlit's st.metric
    # delta string accepts a +/- sign; we render an explicit arrow glyph
    # too so users skimming the sidebar pick up direction at a glance.
    current_week, prior_week, pct = _trend(daily)
    if pct is None and prior_week == 0 and current_week == 0:
        st.caption("Week-over-week: no data yet.")
    elif pct is None:
        st.caption(f"This week ${current_week:,.2f} (prior week $0.00).")
    else:
        arrow = "up" if pct > 0 else ("down" if pct < 0 else "flat")
        sign = "+" if pct > 0 else ""
        st.caption(
            f"Week-over-week: {arrow} {sign}{pct:.1f}% "
            f"(this week ${current_week:,.2f}, prior ${prior_week:,.2f})."
        )

    # Top 5 most expensive prompts in the last 7 days. Audit entries are
    # the source of truth here; cost.py only tracks per-day totals.
    if audit_entries is None:
        audit_entries = _recent_audit_entries(email, _AUDIT_PULL_LIMIT)
    top = _top_prompts(audit_entries, days=7, k=_TOP_PROMPT_LIMIT)
    if top:
        st.caption("Top prompts by cost (last 7 days)")
        for item in top:
            preview = item["preview"]
            if len(preview) > _PREVIEW_CHARS:
                preview = preview[:_PREVIEW_CHARS] + "..."
            st.markdown(f"- `${item['cost']:.4f}` {preview}")
    else:
        st.caption("No costed prompts in audit log for the last 7 days.")

    # Cache savings call-out. Session-scoped (Streamlit reruns share the
    # same _session dict in cost.py) so users see immediate feedback as
    # generations land; persistent savings live in the daily totals.
    saved = _cache_savings(email)
    if saved > 0:
        st.success(f"You saved ${saved:.4f} this session via prompt caching.")
    else:
        st.caption("Cache savings will appear here once cached prefixes are reused.")
