"""Tests for cost.py, Phase 21b per-actor quota accounting.

Verifies:
  - quota_used_by_actor(actor_id) is per-actor (not a global sum)
  - today_usd / record / wrap_client all key on the same actor hash
  - The session accumulator does not bleed between actors
  - clear_session resets in-memory state without touching disk

No real Anthropic SDK calls; we feed mock Usage dicts directly to
cost.record.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import cost


def setup_function(_fn):
    """Reset in-process state before every test."""
    cost.clear_session()
    # Force the local-file path (no GitHub token) so we don't try to
    # write to a real repo during tests.
    cost.configure("", "")


def _usage(in_t: int = 0, out_t: int = 0, cwrite: int = 0, cread: int = 0):
    return SimpleNamespace(
        input_tokens=in_t,
        output_tokens=out_t,
        cache_creation_input_tokens=cwrite,
        cache_read_input_tokens=cread,
    )


def test_quota_used_by_actor_alias_matches_today_usd():
    """Phase 21b: quota_used_by_actor is an alias for today_usd."""
    actor = "service-account-test-EXAMPLE-1"
    a = cost.quota_used_by_actor(actor)
    b = cost.today_usd(actor)
    assert a == b, f"quota_used_by_actor and today_usd must agree, got {a} vs {b}"


def test_session_accumulator_is_per_actor():
    """Two actors recording usage on the same process get separate totals."""
    actor_a = "actor-a-EXAMPLE"
    actor_b = "actor-b-EXAMPLE"
    cost.record(actor_a, _usage(in_t=1_000_000, out_t=0))   # $1.00
    cost.record(actor_b, _usage(in_t=2_000_000, out_t=0))   # $2.00

    a = cost.total_session(actor_a)
    b = cost.total_session(actor_b)
    assert a["calls"] == 1, f"expected 1 call for actor_a, got {a['calls']}"
    assert b["calls"] == 1, f"expected 1 call for actor_b, got {b['calls']}"
    assert abs(a["usd"] - 1.0) < 1e-6, f"actor_a cost should be ~$1.00, got {a['usd']}"
    assert abs(b["usd"] - 2.0) < 1e-6, f"actor_b cost should be ~$2.00, got {b['usd']}"


def test_clear_session_resets_in_memory_only():
    """clear_session zeroes the session dict for every actor."""
    cost.record("actor-X-EXAMPLE", _usage(in_t=500_000))
    assert cost.total_session("actor-X-EXAMPLE")["calls"] == 1
    cost.clear_session()
    assert cost.total_session("actor-X-EXAMPLE")["calls"] == 0


def test_wrap_client_records_per_actor(tmp_path, monkeypatch):
    """wrap_client intercepts messages.create and credits the right actor."""
    # Steer the local-file path to a tmp file so this test never touches
    # the developer's real .streamlit/usage_local.json.
    monkeypatch.setattr(cost, "_LOCAL_PATH", str(tmp_path / "usage.json"))

    class FakeMessages:
        def create(self, **_kw):
            return SimpleNamespace(usage=_usage(in_t=1_000_000))

    class FakeClient:
        def __init__(self):
            self.messages = FakeMessages()

    wrapped = cost.wrap_client(FakeClient(), "actor-wrap-EXAMPLE")
    wrapped.messages.create(model="claude-haiku-4-5-20251001", max_tokens=10)
    sess = cost.total_session("actor-wrap-EXAMPLE")
    assert sess["calls"] == 1
    assert sess["input"] == 1_000_000


def test_record_returns_priced_cost():
    """record() returns the dollar cost of the call so audit can attribute."""
    delta = cost.record("actor-price-EXAMPLE", _usage(in_t=1_000_000, out_t=200_000))
    # $1/M input + $5/M output -> 1.00 + 1.00 = 2.00
    assert abs(delta - 2.0) < 1e-6, f"expected ~$2.00, got {delta}"


def test_today_usd_unknown_actor_is_zero(tmp_path, monkeypatch):
    """An actor with no recorded usage returns 0.0, not an error."""
    monkeypatch.setattr(cost, "_LOCAL_PATH", str(tmp_path / "usage.json"))
    assert cost.today_usd("never-seen-actor-EXAMPLE") == 0.0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            setup_function(fn)
            # Crude tmp_path / monkeypatch shim for standalone runs.
            import inspect
            sig = inspect.signature(fn)
            kwargs = {}
            if "tmp_path" in sig.parameters:
                import tempfile
                from pathlib import Path as _P
                kwargs["tmp_path"] = _P(tempfile.mkdtemp())
            if "monkeypatch" in sig.parameters:
                class _MP:
                    def __init__(self):
                        self._undo = []
                    def setattr(self, obj, name, value):
                        old = getattr(obj, name)
                        setattr(obj, name, value)
                        self._undo.append((obj, name, old))
                    def undo(self):
                        for obj, name, old in self._undo:
                            setattr(obj, name, old)
                mp = _MP()
                kwargs["monkeypatch"] = mp
            try:
                fn(**kwargs)
            finally:
                if "monkeypatch" in kwargs:
                    kwargs["monkeypatch"].undo()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(0 if failures == 0 else 1)
