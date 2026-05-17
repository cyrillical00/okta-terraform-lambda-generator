"""Tests for `headless_rate_limit` module.

Standalone-runnable: `python tests/test_headless_rate_limit.py` prints
PASS/FAIL per test without any pytest dependency. Pytest discovers the
same `test_*` functions if installed.

Coverage:
- Single-actor under-cap: 30 sequential calls succeed inside the window
  for the HTTP limiter at its 30-rpm default.
- Single-actor over-cap: the 31st call denies with positive retry_after.
- TTL eviction: after the window passes, the actor can call again and
  the dq returns to empty.
- Multi-actor isolation: actor A hitting its cap does not prevent
  actor B from calling.
- retry_after math: returned seconds match `deque[0] + window - now`
  within a 1s ceil tolerance.
- check_input_length under-cap returns (True, "") and over-cap returns
  (False, informative message containing both lengths).
- Slack / JIRA singletons cap at 20 rpm (lower than HTTP's 30).
- Constructor honors a custom window and rpm.
- `reset()` clears state for one actor and for all actors.
- Thread safety smoke: 4 threads racing on one actor cannot exceed
  the cap.
"""

from __future__ import annotations

import os
import sys
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import headless_rate_limit as hrl
from headless_rate_limit import (
    HTTP_RATE_LIMITER,
    INPUT_LENGTH_CAP_BYTES,
    JIRA_RATE_LIMITER,
    RateLimiter,
    SLACK_RATE_LIMITER,
    check_input_length,
)


# ── RateLimiter behaviour ────────────────────────────────────────────────


def test_http_limiter_30_calls_all_allowed():
    """First 30 sequential calls inside the window all succeed."""
    rl = RateLimiter(requests_per_minute=30, window_seconds=60)
    for i in range(30):
        allowed, retry = rl.check("actor-a")
        assert allowed, f"call {i+1} denied unexpectedly: retry={retry}"
        assert retry == 0, f"call {i+1} reported retry_after={retry} on allow"


def test_http_limiter_31st_call_denied_with_positive_retry():
    """31st call inside the window is denied with retry_after >= 1."""
    rl = RateLimiter(requests_per_minute=30, window_seconds=60)
    for _ in range(30):
        rl.check("actor-a")
    allowed, retry = rl.check("actor-a")
    assert not allowed, "31st call should have been rate limited"
    assert retry >= 1, f"retry_after should be >= 1, got {retry}"
    assert retry <= 60, f"retry_after should be <= window, got {retry}"


def test_ttl_eviction_allows_calls_after_window_passes():
    """Once the window passes, stale timestamps are evicted and the actor
    can call again. Uses a short window so the test runs quickly."""
    rl = RateLimiter(requests_per_minute=2, window_seconds=1)
    a1, _ = rl.check("actor-a")
    a2, _ = rl.check("actor-a")
    a3, retry = rl.check("actor-a")
    assert a1 and a2 and not a3, f"expected allow/allow/deny, got {a1}/{a2}/{a3}"
    assert retry >= 1
    # Sleep past the window so the two earlier entries age out.
    time.sleep(1.2)
    a4, _ = rl.check("actor-a")
    assert a4, "after TTL eviction the actor should be allowed again"


def test_multi_actor_isolation():
    """Actor A hitting its limit must not block actor B."""
    rl = RateLimiter(requests_per_minute=2, window_seconds=60)
    rl.check("actor-a")
    rl.check("actor-a")
    blocked, _ = rl.check("actor-a")
    assert not blocked, "actor-a should be blocked after 2 calls"
    # actor-b should be unaffected.
    b1, _ = rl.check("actor-b")
    b2, _ = rl.check("actor-b")
    assert b1 and b2, "actor-b should be allowed regardless of actor-a's state"


def test_retry_after_math_matches_window_minus_elapsed():
    """retry_after must equal ceil(oldest + window - now). With a 2s
    window and one immediate call, the next denied call should report
    retry_after in {1, 2}, not 60 and not 0."""
    rl = RateLimiter(requests_per_minute=1, window_seconds=2)
    t0 = time.monotonic()
    rl.check("actor-a")  # fills the window
    _, retry = rl.check("actor-a")  # immediately denied
    elapsed = time.monotonic() - t0
    expected_upper = 2 - elapsed + 1  # +1 for ceil
    assert 1 <= retry <= 2, f"retry_after {retry} not in [1, 2] for 2s window"
    assert retry <= expected_upper + 1, (
        f"retry_after {retry} exceeds expected ceiling {expected_upper}"
    )


def test_reset_clears_one_actor():
    """reset(actor) wipes only that actor's window."""
    rl = RateLimiter(requests_per_minute=1, window_seconds=60)
    rl.check("actor-a")
    rl.check("actor-b")
    assert not rl.check("actor-a")[0]
    rl.reset("actor-a")
    assert rl.check("actor-a")[0], "after reset actor-a should be allowed"
    # actor-b's state is untouched.
    assert not rl.check("actor-b")[0], "actor-b must remain rate limited"


def test_reset_all_clears_every_actor():
    """reset() with no arg wipes every actor."""
    rl = RateLimiter(requests_per_minute=1, window_seconds=60)
    rl.check("actor-a")
    rl.check("actor-b")
    rl.reset()
    assert rl.check("actor-a")[0]
    assert rl.check("actor-b")[0]


def test_thread_safe_concurrent_actors_do_not_exceed_cap():
    """Four threads racing on the same actor must not exceed the cap.
    Each thread issues 100 calls; the total allowed count across all
    threads must equal the per-actor cap."""
    rl = RateLimiter(requests_per_minute=10, window_seconds=60)
    counter = {"allowed": 0}
    lock = threading.Lock()

    def worker():
        local = 0
        for _ in range(100):
            ok, _ = rl.check("shared-actor")
            if ok:
                local += 1
        with lock:
            counter["allowed"] += local

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert counter["allowed"] == 10, (
        f"expected exactly 10 allowed across all threads, got {counter['allowed']}"
    )


# ── check_input_length ───────────────────────────────────────────────────


def test_input_length_under_cap_allowed():
    ok, reason = check_input_length("short prompt", max_bytes=8192)
    assert ok is True
    assert reason == ""


def test_input_length_over_cap_denied_with_informative_message():
    big = "x" * 10000
    ok, reason = check_input_length(big, max_bytes=8192)
    assert ok is False
    assert "10000" in reason, f"reason should mention input length: {reason}"
    assert "8192" in reason, f"reason should mention cap: {reason}"


def test_input_length_measures_utf8_bytes_not_chars():
    """A 3-char multibyte string is 9 bytes in UTF-8, so it should fail a
    5-byte cap even though its character length is 3."""
    # Three U+1F600 grinning-face emoji: 4 bytes each = 12 bytes UTF-8.
    text = "\U0001f600\U0001f600\U0001f600"
    assert len(text) == 3
    ok, reason = check_input_length(text, max_bytes=5)
    assert ok is False
    assert "12" in reason, f"reason should reflect 12 UTF-8 bytes: {reason}"


def test_input_length_none_safe():
    """A None input should not crash; treat as empty and allow."""
    ok, reason = check_input_length(None, max_bytes=8192)  # type: ignore[arg-type]
    assert ok is True
    assert reason == ""


# ── Module-level singletons ──────────────────────────────────────────────


def test_slack_singleton_uses_lower_cap_than_http():
    """SLACK_RATE_LIMITER must be configured at 20 rpm, lower than HTTP's 30."""
    assert SLACK_RATE_LIMITER._rpm == 20, (
        f"SLACK_RATE_LIMITER rpm should be 20, got {SLACK_RATE_LIMITER._rpm}"
    )
    assert HTTP_RATE_LIMITER._rpm == 30
    assert SLACK_RATE_LIMITER._rpm < HTTP_RATE_LIMITER._rpm


def test_jira_singleton_uses_lower_cap_than_http():
    """JIRA_RATE_LIMITER must be configured at 20 rpm, lower than HTTP's 30."""
    assert JIRA_RATE_LIMITER._rpm == 20, (
        f"JIRA_RATE_LIMITER rpm should be 20, got {JIRA_RATE_LIMITER._rpm}"
    )
    assert JIRA_RATE_LIMITER._rpm < HTTP_RATE_LIMITER._rpm


def test_input_length_cap_constant():
    """The module-level cap is 8192 (8 KiB)."""
    assert INPUT_LENGTH_CAP_BYTES == 8192


# ── Standalone runner ────────────────────────────────────────────────────


if __name__ == "__main__":
    import traceback

    tests = [
        test_http_limiter_30_calls_all_allowed,
        test_http_limiter_31st_call_denied_with_positive_retry,
        test_ttl_eviction_allows_calls_after_window_passes,
        test_multi_actor_isolation,
        test_retry_after_math_matches_window_minus_elapsed,
        test_reset_clears_one_actor,
        test_reset_all_clears_every_actor,
        test_thread_safe_concurrent_actors_do_not_exceed_cap,
        test_input_length_under_cap_allowed,
        test_input_length_over_cap_denied_with_informative_message,
        test_input_length_measures_utf8_bytes_not_chars,
        test_input_length_none_safe,
        test_slack_singleton_uses_lower_cap_than_http,
        test_jira_singleton_uses_lower_cap_than_http,
        test_input_length_cap_constant,
    ]
    failures: list[str] = []
    for t in tests:
        # Reset singletons before each test so prior test state can't
        # leak. The singleton-cap tests inspect _rpm only, so reset is
        # safe there too.
        HTTP_RATE_LIMITER.reset()
        SLACK_RATE_LIMITER.reset()
        JIRA_RATE_LIMITER.reset()
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception:
            failures.append(t.__name__)
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    if failures:
        print(f"\n{len(failures)} failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} passed")
