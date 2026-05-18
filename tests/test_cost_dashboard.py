"""Tests for ui/cost_dashboard.py (Phase 22a).

Verifies the dashboard renders without errors on:
  - empty state (no usage history, no audit entries)
  - populated state (synthetic daily totals + audit entries)

We don't validate visual layout. We validate that:
  1. The internal helpers compute the right windows / aggregates.
  2. render_cost_dashboard runs end-to-end against a Streamlit stub
     without raising.

Streamlit is stubbed locally (st.metric, st.columns, st.bar_chart,
st.caption, st.markdown, st.success) so the test does not depend on
streamlit being importable in the test env.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import cost as _cost
from ui import cost_dashboard


# ── streamlit stub ───────────────────────────────────────────────────────


class _Calls:
    def __init__(self):
        self.metric: list[tuple] = []
        self.caption: list[str] = []
        self.markdown: list[str] = []
        self.bar_chart: list[dict] = []
        self.success: list[str] = []
        self.columns_invocations: int = 0


class _ColumnCtx:
    def __init__(self, calls: _Calls):
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def metric(self, label, value, **kw):
        self._calls.metric.append((label, value, kw))


class _StStub:
    def __init__(self):
        self.calls = _Calls()

    def columns(self, n):
        self.calls.columns_invocations += 1
        return tuple(_ColumnCtx(self.calls) for _ in range(n))

    def metric(self, label, value, **kw):
        self.calls.metric.append((label, value, kw))

    def caption(self, text):
        self.calls.caption.append(text)

    def markdown(self, text, **kw):
        self.calls.markdown.append(text)

    def bar_chart(self, data, **kw):
        self.calls.bar_chart.append(dict(data))

    def success(self, text):
        self.calls.success.append(text)


def _install_st_stub(monkeypatch):
    stub = _StStub()
    monkeypatch.setitem(sys.modules, "streamlit", stub)
    return stub


# ── fixtures ─────────────────────────────────────────────────────────────


def _write_local_usage(tmp_path, email: str, daily: dict[str, float]) -> str:
    """Write a synthetic usage_local.json under tmp_path and point cost
    dashboard helpers at it via monkeypatch in the test body."""
    p = tmp_path / "usage_local.json"
    payload = {email: daily}
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


def setup_function(_fn):
    _cost.clear_session()
    _cost.configure("", "")


# ── unit-level helper coverage ───────────────────────────────────────────


def test_build_window_returns_n_days_in_order():
    today = datetime.now(timezone.utc).date()
    keys = [
        (today - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(6, -1, -1)
    ]
    window = cost_dashboard._build_window({keys[3]: 1.0}, 7)
    assert len(window) == 7, f"expected 7 entries, got {len(window)}"
    labels = [w[0] for w in window]
    assert labels == keys, f"window order mismatch: {labels} vs {keys}"
    # Days without spend default to 0.0.
    assert window[3][1] == 1.0
    assert window[0][1] == 0.0


def test_trend_pct_when_prior_is_zero():
    today = datetime.now(timezone.utc).date()
    daily = {(today - timedelta(days=i)).strftime("%Y-%m-%d"): 1.0 for i in range(0, 7)}
    current, prior, pct = cost_dashboard._trend(daily)
    assert pct is None, "prior-week-zero should yield pct=None"
    assert prior == 0.0
    assert current == 7.0


def test_trend_pct_positive_growth():
    today = datetime.now(timezone.utc).date()
    daily: dict[str, float] = {}
    # Prior week (days 7..13): $1/day = $7 total
    for i in range(7, 14):
        daily[(today - timedelta(days=i)).strftime("%Y-%m-%d")] = 1.0
    # Current week (days 0..6): $2/day = $14 total -> +100%
    for i in range(0, 7):
        daily[(today - timedelta(days=i)).strftime("%Y-%m-%d")] = 2.0
    current, prior, pct = cost_dashboard._trend(daily)
    assert prior == 7.0
    assert current == 14.0
    assert pct is not None and abs(pct - 100.0) < 0.001


def test_top_prompts_filters_and_ranks():
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=30)).isoformat()
    recent = now.isoformat()
    entries = [
        {"timestamp_utc": old, "cost_estimate_usd": 9.99, "redacted_input_preview": "ancient expensive prompt"},
        {"timestamp_utc": recent, "cost_estimate_usd": 0.02, "redacted_input_preview": "cheap"},
        {"timestamp_utc": recent, "cost_estimate_usd": 0.50, "redacted_input_preview": "mid"},
        {"timestamp_utc": recent, "cost_estimate_usd": 1.00, "redacted_input_preview": "top"},
        {"timestamp_utc": recent, "cost_estimate_usd": 0.0, "redacted_input_preview": "zero cost should drop"},
        {"timestamp_utc": recent, "cost_estimate_usd": 0.30, "redacted_input_preview": ""},
    ]
    top = cost_dashboard._top_prompts(entries, days=7, k=5)
    previews = [t["preview"] for t in top]
    assert previews == ["top", "mid", "cheap"], f"ranking mismatch: {previews}"


def test_cache_savings_session_only():
    actor = "actor-cache-EXAMPLE"
    # 10M cache reads -> $9 saved (delta of $0.90/M)
    cost_dashboard._cost.record(actor, SimpleNamespace(
        input_tokens=0, output_tokens=0,
        cache_creation_input_tokens=0, cache_read_input_tokens=10_000_000,
    ))
    saved = cost_dashboard._cache_savings(actor)
    assert abs(saved - 9.0) < 0.001, f"expected ~9.0 saved, got {saved}"


def test_cache_hit_rate_returns_none_when_no_traffic():
    rate = cost_dashboard._cache_hit_rate("nobody-EXAMPLE")
    assert rate is None


def test_cache_hit_rate_basic_math():
    actor = "actor-rate-EXAMPLE"
    cost_dashboard._cost.record(actor, SimpleNamespace(
        input_tokens=1_000, output_tokens=0,
        cache_creation_input_tokens=0, cache_read_input_tokens=3_000,
    ))
    rate = cost_dashboard._cache_hit_rate(actor)
    assert rate is not None
    assert abs(rate - 75.0) < 0.1, f"expected ~75%, got {rate}"


# ── end-to-end render: empty state ───────────────────────────────────────


def test_render_empty_state_does_not_raise(monkeypatch, tmp_path):
    """No usage_local.json + no audit -> empty state renders cleanly."""
    stub = _install_st_stub(monkeypatch)
    # Point the dashboard at a tmp file that doesn't exist.
    monkeypatch.setattr(cost_dashboard, "_LOCAL_USAGE_PATH", str(tmp_path / "missing.json"))
    # Make audit.recent fail closed -> empty list.
    monkeypatch.setattr(cost_dashboard, "_recent_audit_entries", lambda *_a, **_k: [])

    cost_dashboard.render_cost_dashboard({}, "empty@example.com")

    # Three metric cards from row 1
    assert len(stub.calls.metric) == 3, f"expected 3 metric cards, got {len(stub.calls.metric)}"
    labels = [m[0] for m in stub.calls.metric]
    assert "Today" in labels and "Prompts (session)" in labels and "Cache hit rate" in labels
    # No bar chart on empty state
    assert stub.calls.bar_chart == []
    # Empty-state captions render
    joined = " | ".join(stub.calls.caption)
    assert "No spend recorded" in joined
    assert "No costed prompts" in joined


# ── end-to-end render: populated state ───────────────────────────────────


def test_render_populated_state_renders_chart_and_top_prompts(monkeypatch, tmp_path):
    stub = _install_st_stub(monkeypatch)

    actor = "populated@example.com"
    today = datetime.now(timezone.utc).date()

    # Seed daily totals for the last 14 days.
    daily = {}
    for i in range(0, 14):
        daily[(today - timedelta(days=i)).strftime("%Y-%m-%d")] = 1.5 + i * 0.1
    local_path = _write_local_usage(tmp_path, actor, daily)
    monkeypatch.setattr(cost_dashboard, "_LOCAL_USAGE_PATH", local_path)

    # Seed audit entries: one expensive, one cheap.
    now_iso = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(
        cost_dashboard,
        "_recent_audit_entries",
        lambda *_a, **_k: [
            {"timestamp_utc": now_iso, "cost_estimate_usd": 0.25, "redacted_input_preview": "create okta group Engineering"},
            {"timestamp_utc": now_iso, "cost_estimate_usd": 0.08, "redacted_input_preview": "create a contractors group"},
        ],
    )

    # Seed a cache-read session so the savings call-out fires.
    cost_dashboard._cost.record(actor, SimpleNamespace(
        input_tokens=0, output_tokens=0,
        cache_creation_input_tokens=0, cache_read_input_tokens=5_000_000,
    ))

    cost_dashboard.render_cost_dashboard({}, actor)

    # Bar chart rendered.
    assert len(stub.calls.bar_chart) == 1, "expected one bar chart for 7-day window"
    chart = stub.calls.bar_chart[0]
    assert len(chart) == 7

    # Top prompts list rendered.
    top_lines = [m for m in stub.calls.markdown if "create" in m.lower()]
    assert len(top_lines) >= 2, f"expected top-prompt markdown entries, got {stub.calls.markdown!r}"

    # Cache savings success call-out fired (5M reads -> $4.50 saved).
    assert len(stub.calls.success) == 1
    assert "saved" in stub.calls.success[0].lower()
