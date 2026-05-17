"""In-memory sliding-window rate limiter for the headless surfaces
(HTTP API, Slack handler, JIRA handler).

Pure-Python; no Redis / external state. Uses `dict[actor_id, deque[ts]]`
with TTL eviction on every check call so memory stays bounded. The
trade-off is that horizontally-scaled deploys (multiple Vercel
instances behind a load balancer) will have per-instance limits, not
global. Acceptable today because each headless surface deploys to a
single Vercel function with sticky cold-start. Phase 19 candidate:
move to Vercel KV / Redis if multi-instance scale becomes real.

Two public primitives:

* `RateLimiter.check(actor_id)` returns `(allowed, retry_after_seconds)`.
  Thread-safe: a `threading.Lock` guards the per-actor deque mutation
  so concurrent Vercel handler invocations on the same warm worker
  cannot race on the same actor's window.
* `check_input_length(text, max_bytes)` is a stateless helper that
  rejects prompts over the byte cap (UTF-8 measured, since multibyte
  characters consume more model tokens than ASCII).

Module-level singletons configure per-surface limits so the wiring
sites stay one-liners. HTTP gets 30 rpm because API callers typically
batch multiple generate calls per JIRA ticket; Slack and JIRA get 20
rpm because each request triggers a user-facing message and
flooding a channel / issue is worse than rate-limiting a script.
"""

from __future__ import annotations

import math
import time
from collections import deque
from threading import Lock


class RateLimiter:
    """Sliding-window rate limiter keyed by actor id.

    Window is `window_seconds` wide and allows up to `requests_per_minute`
    requests inside it. (Naming follows convention even though the
    window need not be exactly 60s; tests use a 1s window for speed.)
    """

    def __init__(self, requests_per_minute: int = 30, window_seconds: int = 60):
        self._rpm = int(requests_per_minute)
        self._window = float(window_seconds)
        self._actors: dict[str, deque[float]] = {}
        self._lock = Lock()

    def check(self, actor_id: str) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds).

        allowed=True + retry_after=0 means the request can proceed.
        allowed=False + retry_after=N means rate limited; client should
        wait N seconds before retrying.

        Eviction of stale timestamps happens on every call so memory
        stays bounded by the active-actor count, not historical
        traffic. A pathologically-large actor population is the only
        scenario in which this leaks; even then, the deques empty as
        soon as their entries age out.
        """
        actor_key = actor_id or "anonymous"
        now = time.monotonic()
        cutoff = now - self._window

        with self._lock:
            dq = self._actors.get(actor_key)
            if dq is None:
                dq = deque()
                self._actors[actor_key] = dq

            # Evict timestamps older than the window. popleft is O(1)
            # and the deque stays sorted because we only ever append now.
            while dq and dq[0] <= cutoff:
                dq.popleft()

            if len(dq) < self._rpm:
                dq.append(now)
                return (True, 0)

            # Limit reached. Compute when the oldest entry will age out;
            # retry_after is the ceiling so callers always wait at least
            # 1s and never poll-spin on a sub-second remainder.
            oldest = dq[0]
            wait = (oldest + self._window) - now
            retry_after = max(1, int(math.ceil(wait)))
            return (False, retry_after)

    def reset(self, actor_id: str | None = None) -> None:
        """Test helper. Wipes one actor's window, or all actors when no
        id is given. Not used in production paths."""
        with self._lock:
            if actor_id is None:
                self._actors.clear()
            else:
                self._actors.pop(actor_id, None)


def check_input_length(text: str, max_bytes: int = 8192) -> tuple[bool, str]:
    """Reject prompts whose UTF-8 byte length exceeds `max_bytes`.

    Returns (True, "") when within the cap; (False, reason) otherwise.
    Byte length (not character count) is what matters because Anthropic
    tokenization scales with bytes for non-ASCII content, and the cap
    exists to bound model-input cost / latency.
    """
    if text is None:
        return (True, "")
    n = len(text.encode("utf-8"))
    if n > max_bytes:
        return (False, f"input length {n} bytes exceeds cap {max_bytes}")
    return (True, "")


# ── Per-surface singletons ───────────────────────────────────────────────

# HTTP API: 30 rpm. Higher because automated callers (CI pipelines,
# JIRA Automation rules calling /api/generate directly) batch many
# requests per workflow.
HTTP_RATE_LIMITER = RateLimiter(requests_per_minute=30, window_seconds=60)

# Slack: 20 rpm per user. A single user spamming /tfgen would flood
# the channel; this caps the noise even if the underlying generator
# could handle more.
SLACK_RATE_LIMITER = RateLimiter(requests_per_minute=20, window_seconds=60)

# JIRA: 20 rpm per accountId. JIRA Automation rules can fire dozens
# of webhooks on a bulk label change; capping prevents one bulk
# operation from consuming a day's worth of quota in a minute.
JIRA_RATE_LIMITER = RateLimiter(requests_per_minute=20, window_seconds=60)

# 8KiB UTF-8 cap. Real prompts in production sit at ~500-2000 bytes;
# 8KiB is generous headroom but bounds the worst-case copy-paste of a
# whole runbook into the slash command or webhook description.
INPUT_LENGTH_CAP_BYTES = 8192
