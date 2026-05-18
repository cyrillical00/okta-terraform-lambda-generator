"""Structured JSON-line logging helpers (Phase 22c).

Streamlit Community Cloud's log viewer parses JSON-formatted lines off
stdout/stderr and indexes them by field. This module emits one JSON
line per call to stderr, so operators can grep on `event=...` or
`actor_id=...` in the Cloud log viewer.

Why stderr: stdout is Streamlit's UI surface (Streamlit Cloud captures
both, but stderr is the unambiguous platform-log channel and avoids
any risk of a log line landing in the rendered app).

Public API:
  - log_info(event, **kwargs)
  - log_warn(event, **kwargs)
  - log_error(event, **kwargs)

Each call writes one line of the shape:
  {"ts": "2026-05-18T21:14:02Z", "level": "info", "event": "generate_complete",
   "actor_id": "alice@example.com", "cost": 0.0142, ...}

The `event` field is required. All other kwargs are merged in as
top-level keys, so a Cloud operator can grep on any field directly.
None values are dropped. Non-JSON-serializable values are coerced to
str(...) so the call cannot raise on weird types.

Logging is best-effort: a write failure on stderr never raises.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any


_LEVELS = ("info", "warn", "error")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coerce(v: Any) -> Any:
    """Make sure every value is JSON-serializable. Coerce unknowns to str."""
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return [_coerce(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _coerce(val) for k, val in v.items()}
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return str(v)


def _emit(level: str, event: str, fields: dict[str, Any]) -> None:
    if level not in _LEVELS:
        level = "info"
    record: dict[str, Any] = {
        "ts": _now_iso(),
        "level": level,
        "event": event or "unknown",
    }
    for k, v in fields.items():
        if v is None:
            continue
        record[str(k)] = _coerce(v)
    try:
        line = json.dumps(record, separators=(",", ":"))
    except (TypeError, ValueError):
        # Last-resort fallback: stringify the whole thing so a logging
        # call cannot break a generate / push pipeline.
        line = json.dumps({"ts": _now_iso(), "level": level, "event": event, "error": "serialization_failed"})
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        # stderr write failure (closed stream, broken pipe) -> swallow.
        pass


def log_info(event: str, **kwargs: Any) -> None:
    """Emit one JSON line at level=info. Never raises."""
    _emit("info", event, kwargs)


def log_warn(event: str, **kwargs: Any) -> None:
    """Emit one JSON line at level=warn. Never raises."""
    _emit("warn", event, kwargs)


def log_error(event: str, **kwargs: Any) -> None:
    """Emit one JSON line at level=error. Never raises."""
    _emit("error", event, kwargs)
