"""Per-call Anthropic usage interceptor + per-user daily cost accumulator.

Wraps an `anthropic.Anthropic` client so every `messages.create` call is
priced at Haiku 4.5 rates and accumulated:
  - in-process (per session, via session_state when used from Streamlit)
  - persisted (per UTC day, per user, via the same GitHub-backed pattern
    audit.py and history.py use)

Public API:
  - `wrap_client(client) -> wrapped client`
  - `today_usd(email) -> float`
  - `total_session(email) -> dict` (token + dollar breakdown for this session)
  - `clear_session()` (test convenience)

Pricing constants match qa_runner.py and the README — Haiku 4.5:
  $1/M input, $5/M output, $1.25/M cache write, $0.10/M cache read.

Module load is side-effect free. Configure once via `cost.configure(...)`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone

# Haiku 4.5 pricing (USD per million tokens). Update in lockstep with the README.
PRICE_INPUT_PER_M = 1.00
PRICE_OUTPUT_PER_M = 5.00
PRICE_CACHE_WRITE_PER_M = 1.25
PRICE_CACHE_READ_PER_M = 0.10

_GH_DIR = "_tftool/usage"
_LOCAL_PATH = ".streamlit/usage_local.json"

_github_token: str = ""
_github_repo: str = ""

# In-memory per-process accumulator. Streamlit reruns share this across
# the same browser session because the module survives reruns; full
# isolation only matters for test contexts (clear_session() handles that).
_session: dict[str, dict[str, float]] = {}

# Spend this process could not write to the store that reads come from,
# keyed by (actor hash, UTC day). It is added to every read so a failed write
# is still counted against the quota, at least by the worker that incurred it.
_unpersisted: dict[tuple[str, str], float] = {}


class UsageUnavailable(RuntimeError):
    """Today's spend cannot be read, or there is nowhere to record it.

    Raised by `today_usd(..., strict=True)`. Quota checks on the headless
    surfaces treat it as "blocked": an unknown total must not count as zero.
    """


def configure(github_token: str, github_repo: str) -> None:
    global _github_token, _github_repo
    _github_token = (github_token or "").strip()
    _github_repo = (github_repo or "").strip()


def clear_session() -> None:
    _session.clear()
    _unpersisted.clear()


def _email_hash(email: str) -> str:
    return hashlib.sha256((email or "anonymous").encode("utf-8")).hexdigest()[:16]


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _gh_path(email: str) -> str:
    return f"{_GH_DIR}/{_email_hash(email)}.json"


def _price(usage: dict) -> float:
    """Compute USD cost from an anthropic Usage object's fields."""
    if not usage:
        return 0.0
    inp = float(usage.get("input_tokens", 0) or 0)
    out = float(usage.get("output_tokens", 0) or 0)
    cwrite = float(usage.get("cache_creation_input_tokens", 0) or 0)
    cread = float(usage.get("cache_read_input_tokens", 0) or 0)
    return (
        inp * PRICE_INPUT_PER_M / 1_000_000.0
        + out * PRICE_OUTPUT_PER_M / 1_000_000.0
        + cwrite * PRICE_CACHE_WRITE_PER_M / 1_000_000.0
        + cread * PRICE_CACHE_READ_PER_M / 1_000_000.0
    )


def _usage_to_dict(usage_obj) -> dict:
    """Anthropic SDK returns a Usage object with attribute access. Convert
    to a dict so _price can use .get(). Tolerant to None / missing attrs."""
    if usage_obj is None:
        return {}
    if isinstance(usage_obj, dict):
        return usage_obj
    out = {}
    for attr in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        out[attr] = getattr(usage_obj, attr, 0) or 0
    return out


def record(email: str, usage_obj) -> float:
    """Record one API call's usage. Returns the call's USD cost."""
    usage = _usage_to_dict(usage_obj)
    cost = _price(usage)
    sess = _session.setdefault(email or "anonymous", {"calls": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "usd": 0.0})
    sess["calls"] += 1
    sess["input"] += usage.get("input_tokens", 0)
    sess["output"] += usage.get("output_tokens", 0)
    sess["cache_write"] += usage.get("cache_creation_input_tokens", 0)
    sess["cache_read"] += usage.get("cache_read_input_tokens", 0)
    sess["usd"] += cost
    _bump_daily(email, cost)
    return cost


def _bump_daily(email: str, delta_usd: float) -> None:
    """Read-modify-write the user's daily totals JSON. Best-effort: a write
    failure is logged silently rather than blocking the user's request."""
    if delta_usd <= 0:
        return
    today = _today_key()
    if _github_token and _github_repo:
        try:
            _bump_github(email, today, delta_usd)
            return
        except Exception:
            # Reads come from GitHub, so the local fallback below would not
            # be seen by the next quota check. Remember the amount here.
            _note_unpersisted(email, today, delta_usd)
        _bump_local(email, today, delta_usd)
        return
    if not _bump_local(email, today, delta_usd):
        _note_unpersisted(email, today, delta_usd)


def _note_unpersisted(email: str, today: str, delta: float) -> None:
    key = (_email_hash(email), today)
    _unpersisted[key] = round(_unpersisted.get(key, 0.0) + delta, 6)


def _pending(email: str, today: str) -> float:
    return _unpersisted.get((_email_hash(email), today), 0.0)


def _bump_github(email: str, today: str, delta: float) -> None:
    from github import Github, GithubException
    g = Github(_github_token)
    repo = g.get_repo(_github_repo)
    path = _gh_path(email)
    try:
        existing = repo.get_contents(path)
        prev = json.loads(base64.b64decode(existing.content).decode("utf-8"))
        prev[today] = round(prev.get(today, 0.0) + delta, 6)
        repo.update_file(path, "chore(cost): bump daily total", json.dumps(prev, indent=2), existing.sha)
    except GithubException as e:
        if e.status == 404:
            data = {today: round(delta, 6)}
            repo.create_file(path, "chore(cost): create daily total", json.dumps(data, indent=2))
        else:
            raise


def _bump_local(email: str, today: str, delta: float) -> bool:
    """Add `delta` to the local usage file. Returns False if it could not be written."""
    try:
        os.makedirs(os.path.dirname(_LOCAL_PATH), exist_ok=True)
        try:
            with open(_LOCAL_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        per_user = data.setdefault(email or "anonymous", {})
        per_user[today] = round(per_user.get(today, 0.0) + delta, 6)
        with open(_LOCAL_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except OSError:
        return False


def _local_store_writable() -> bool:
    """True if the local usage file could be written (without writing it)."""
    directory = os.path.dirname(_LOCAL_PATH) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return False
    target = _LOCAL_PATH if os.path.exists(_LOCAL_PATH) else directory
    return os.access(target, os.W_OK)


def quota_used_by_actor(actor_id: str) -> float:
    """Phase 21b: per-actor accumulated USD spend for today (UTC).

    Alias for `today_usd` named to match the Phase 21 spec language. The
    underlying tracker is already per-actor (every storage path is keyed
    on `_email_hash(actor_id)`); this name makes the intent explicit at
    HTTP / Slack / JIRA call sites where the parameter is an opaque
    actor_id rather than an email.
    """
    return today_usd(actor_id)


def today_usd(email: str, *, strict: bool = False) -> float:
    """Return the user's accumulated USD spend for today (UTC).

    By default a storage problem reads as 0.0, which suits display code.
    With `strict=True` nothing is guessed: `UsageUnavailable` is raised when
    the stored total cannot be read (any GitHub error other than "no usage
    file yet", or a corrupt local file) and when there is no store the
    counter could be written to. Quota enforcement uses strict mode.
    """
    today = _today_key()
    pending = _pending(email, today)
    if _github_token and _github_repo:
        try:
            from github import Github, GithubException
            g = Github(_github_token)
            repo = g.get_repo(_github_repo)
            try:
                contents = repo.get_contents(_gh_path(email))
                data = json.loads(base64.b64decode(contents.content).decode("utf-8"))
                return float(data.get(today, 0.0) or 0.0) + pending
            except GithubException as e:
                if e.status == 404:
                    return pending
                raise
        except Exception as e:
            if strict:
                raise UsageUnavailable("usage store could not be read") from e
            return pending
    try:
        with open(_LOCAL_PATH, encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(raw) if raw.strip() else {}
        stored = float(data.get(email or "anonymous", {}).get(today, 0.0) or 0.0)
    except FileNotFoundError:
        stored = 0.0
    except (json.JSONDecodeError, OSError, AttributeError, TypeError, ValueError) as e:
        if strict:
            raise UsageUnavailable("local usage file could not be read") from e
        return pending
    if strict and not _local_store_writable():
        raise UsageUnavailable("no writable usage store is configured")
    return stored + pending


def total_session(email: str) -> dict:
    """Return the in-process session totals for the given user."""
    return dict(_session.get(email or "anonymous", {"calls": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "usd": 0.0}))


# ─── client wrapper ────────────────────────────────────────────────────────

def wrap_client(client, email: str):
    """Return a thin proxy that intercepts `messages.create(...)` to record
    usage. The proxy forwards every other attribute to the underlying client.
    """

    class _MessagesProxy:
        def __init__(self, real_messages):
            self._real = real_messages

        def create(self, *args, **kwargs):
            response = self._real.create(*args, **kwargs)
            try:
                record(email, getattr(response, "usage", None))
            except Exception:
                pass
            return response

        def __getattr__(self, name):
            return getattr(self._real, name)

    class _ClientProxy:
        def __init__(self, real, email_local):
            self._real = real
            self.messages = _MessagesProxy(real.messages)
            self._email = email_local

        def __getattr__(self, name):
            return getattr(self._real, name)

    return _ClientProxy(client, email)
