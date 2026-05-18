"""Tests for structured_log.py (Phase 22c).

Verifies the JSON-line format that Streamlit Cloud's log viewer parses
+ that logging is best-effort (never raises on weird input or a
closed stderr).
"""

from __future__ import annotations

import io
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import structured_log


# ── helpers ──────────────────────────────────────────────────────────────


def _capture(monkeypatch) -> io.StringIO:
    """Redirect structured_log's stderr writes into a buffer."""
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    return buf


def _parse_lines(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


# ── tests ────────────────────────────────────────────────────────────────


def test_log_info_emits_json_line(monkeypatch):
    buf = _capture(monkeypatch)
    structured_log.log_info("generate_complete", actor_id="alice@example.com", cost=0.0142)
    lines = _parse_lines(buf)
    assert len(lines) == 1
    rec = lines[0]
    assert rec["level"] == "info"
    assert rec["event"] == "generate_complete"
    assert rec["actor_id"] == "alice@example.com"
    assert rec["cost"] == 0.0142
    # Timestamp present and in UTC iso shape
    assert "ts" in rec and rec["ts"].endswith("Z")


def test_log_warn_and_error_levels(monkeypatch):
    buf = _capture(monkeypatch)
    structured_log.log_warn("audit_sink_flush_retry", attempt=3)
    structured_log.log_error("gh_push_repo_not_found", repo="cyrillical00/missing")
    lines = _parse_lines(buf)
    assert lines[0]["level"] == "warn"
    assert lines[0]["attempt"] == 3
    assert lines[1]["level"] == "error"
    assert lines[1]["repo"] == "cyrillical00/missing"


def test_none_values_are_dropped(monkeypatch):
    buf = _capture(monkeypatch)
    structured_log.log_info("test_event", set_value="x", null_value=None)
    rec = _parse_lines(buf)[0]
    assert "set_value" in rec
    assert "null_value" not in rec


def test_non_serializable_values_coerced(monkeypatch):
    buf = _capture(monkeypatch)
    class Thing:
        def __repr__(self):
            return "<Thing>"
    structured_log.log_info("test_event", obj=Thing())
    rec = _parse_lines(buf)[0]
    # Non-serializable values are coerced to str(...)
    assert rec["obj"] == "<Thing>"


def test_unknown_level_falls_back_to_info(monkeypatch):
    buf = _capture(monkeypatch)
    structured_log._emit("garbage_level", "x", {})
    rec = _parse_lines(buf)[0]
    assert rec["level"] == "info"


def test_event_default_when_empty(monkeypatch):
    buf = _capture(monkeypatch)
    structured_log.log_info("")
    rec = _parse_lines(buf)[0]
    assert rec["event"] == "unknown"


def test_logging_does_not_raise_on_closed_stderr(monkeypatch):
    class _BrokenStderr:
        def write(self, *_a, **_k):
            raise OSError("closed")
        def flush(self):
            raise OSError("closed")

    monkeypatch.setattr(sys, "stderr", _BrokenStderr())
    # Should swallow the error silently; best-effort logging.
    structured_log.log_info("test_event", k="v")
    structured_log.log_warn("test_event")
    structured_log.log_error("test_event")


def test_nested_dict_value_serializes(monkeypatch):
    buf = _capture(monkeypatch)
    structured_log.log_info(
        "complex_event",
        nested={"a": 1, "b": [1, 2, {"c": "d"}]},
    )
    rec = _parse_lines(buf)[0]
    assert rec["nested"]["a"] == 1
    assert rec["nested"]["b"][2]["c"] == "d"
