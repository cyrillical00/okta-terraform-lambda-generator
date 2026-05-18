"""Phase 21c: GitHub-backed write-only audit sink + per-key token issuance.

Two responsibilities:

1. `append_event(event: dict)`: mirror of every audit.log call to a
   monthly JSONL file in the configured GitHub repo. Buffers events in
   memory and flushes on every call; failures (429, 5xx, 404, network)
   are queued and retried on the next call. After 10 consecutive
   failures the buffered batch is dropped with a `print` / `st.error`
   notification. User-facing operations are NEVER blocked by a sink
   failure; this module swallows every exception.

2. CLI helper `python -m audit_github_sink issue --actor-id X --role Y
   --quota-usd N`: generates a fresh `secrets.token_urlsafe(32)` token,
   appends its SHA-256 hash + metadata to roles.toml [api_keys], prints
   the plaintext token ONCE with a "store this now" warning, and logs
   an `api_key_issued` audit event (without the plaintext token).

Storage layout: `_tftool/audit/audit-YYYY-MM.jsonl` in the configured
repo. Auto-rotate on month boundary. Older months are never modified.

Opt-in via `[audit] github_sink_enabled = true` in roles.toml. Default
false so Streamlit Cloud users without the secret get no GitHub sink,
no errors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


# Module-level buffer + state. A single lock guards both. Vercel reuses
# warm workers across requests, so the buffer survives across calls in a
# warm worker and gets garbage-collected on cold start (loss is bounded
# to the last few events).
_BUFFER: list[dict] = []
_LOCK = threading.Lock()
_FAILURE_COUNT = 0
_MAX_FAILURES_BEFORE_DROP = 10

_GH_DEFAULT_PATH_PREFIX = "_tftool/audit/"
_TF_TOOL_VERSION_FALLBACK = "unknown"


# ── roles.toml access ────────────────────────────────────────────────────


def _roles_toml_path() -> Path:
    """Resolve roles.toml at the repo root. Override via TFGEN_ROLES_TOML."""
    override = (os.environ.get("TFGEN_ROLES_TOML") or "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "roles.toml"


def _read_audit_config() -> dict[str, Any]:
    """Return the [audit] table from roles.toml. Empty dict on any error.

    Default `github_sink_enabled = False` makes the feature opt-in.
    """
    path = _roles_toml_path()
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    tbl = data.get("audit") or {}
    if not isinstance(tbl, dict):
        return {}
    return tbl


def _sink_enabled() -> bool:
    cfg = _read_audit_config()
    return bool(cfg.get("github_sink_enabled", False))


def _sink_repo() -> str:
    cfg = _read_audit_config()
    return (cfg.get("github_audit_repo") or "").strip()


def _sink_path_prefix() -> str:
    cfg = _read_audit_config()
    prefix = (cfg.get("github_audit_path_prefix") or _GH_DEFAULT_PATH_PREFIX).strip()
    return prefix if prefix.endswith("/") else prefix + "/"


# ── helpers ──────────────────────────────────────────────────────────────


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _current_month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _tf_tool_version() -> str:
    """Best-effort short git SHA for the running tree.

    Returns the env-var override `TFGEN_VERSION` first; otherwise reads
    `.git/HEAD` / `refs/heads/<branch>` from disk. Never shells out
    (keeps this hot-path import-safe in Vercel cold starts).
    """
    override = (os.environ.get("TFGEN_VERSION") or "").strip()
    if override:
        return override
    try:
        head_path = Path(__file__).resolve().parent / ".git" / "HEAD"
        if not head_path.exists():
            return _TF_TOOL_VERSION_FALLBACK
        head = head_path.read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            ref_path = head_path.parent / ref
            if ref_path.exists():
                return ref_path.read_text(encoding="utf-8").strip()[:7]
            return _TF_TOOL_VERSION_FALLBACK
        return head[:7]
    except OSError:
        return _TF_TOOL_VERSION_FALLBACK


def _canonicalize(event: dict) -> dict:
    """Return the canonical Phase 21c event shape.

    Required keys:
      timestamp, actor_id, action, extra, tf_tool_version.

    Callers (audit.py) pass arbitrary fields; we normalize to the
    canonical shape and stash the rest under `extra`.
    """
    extra = dict(event.get("extra") or {})
    # Move any non-canonical top-level fields into extra so the on-disk
    # shape stays predictable.
    canonical_keys = {"timestamp", "actor_id", "action", "extra", "tf_tool_version"}
    for k, v in event.items():
        if k in canonical_keys:
            continue
        # Don't clobber an extra key that the caller already set.
        extra.setdefault(k, v)
    return {
        "timestamp": event.get("timestamp") or _utc_now_iso(),
        "actor_id": event.get("actor_id") or "anonymous",
        "action": event.get("action") or "",
        "extra": extra,
        "tf_tool_version": event.get("tf_tool_version") or _tf_tool_version(),
    }


def _structured_info(event: str, **kwargs) -> None:
    """Best-effort structured_log.info shim. Import is lazy so callers
    that load this module before structured_log (e.g. in early Vercel
    cold starts) don't hit an import cycle."""
    try:
        import structured_log
        structured_log.log_info(event, **kwargs)
    except Exception:
        pass


def _structured_warn(event: str, **kwargs) -> None:
    try:
        import structured_log
        structured_log.log_warn(event, **kwargs)
    except Exception:
        pass


def _notify_failure(message: str) -> None:
    """Surface a sink failure without raising.

    Prefers `st.error` in a Streamlit context; falls back to a JSON
    structured log line on stderr in headless contexts (CLI, HTTP,
    Slack, JIRA). Never raises.
    """
    try:
        import streamlit as st  # type: ignore[import-not-found]
        st.error(message)
        return
    except Exception:
        pass
    _structured_warn("audit_sink_user_notify", message=message)


# ── core: append_event + flush ───────────────────────────────────────────


def _flush_to_github(events: list[dict]) -> bool:
    """Append `events` to the current month's JSONL file in the audit repo.

    Returns True on success, False on any failure (429, 5xx, 404 on
    repo root, network error). Never raises.
    """
    if not events:
        return True
    repo_name = _sink_repo()
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if not repo_name or not token:
        return False
    try:
        from github import Github, GithubException
    except ImportError:
        return False
    try:
        g = Github(token)
        repo = g.get_repo(repo_name)
    except Exception as e:
        _structured_warn("audit_sink_repo_unreachable", repo=repo_name, error=str(e))
        return False

    path = f"{_sink_path_prefix()}audit-{_current_month_key()}.jsonl"
    new_lines = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events)

    try:
        try:
            existing = repo.get_contents(path)
            import base64
            prev = base64.b64decode(existing.content).decode("utf-8")
            repo.update_file(
                path,
                f"chore(audit): append {len(events)} event(s)",
                prev + new_lines,
                existing.sha,
            )
            _structured_info("audit_sink_flush_success", repo=repo_name, path=path,
                             event_count=len(events), mode="append")
        except GithubException as e:
            status = getattr(e, "status", None)
            if status == 404:
                repo.create_file(
                    path,
                    f"chore(audit): create monthly audit file ({len(events)} event(s))",
                    new_lines,
                )
                _structured_info("audit_sink_flush_success", repo=repo_name, path=path,
                                 event_count=len(events), mode="create")
            else:
                _structured_warn("audit_sink_flush_retry", repo=repo_name, path=path,
                                 event_count=len(events), status=status)
                # 429 / 5xx / other -> caller queues for retry
                return False
    except Exception as e:
        _structured_warn("audit_sink_flush_failure", repo=repo_name, path=path,
                         event_count=len(events), error=str(e))
        return False
    return True


def append_event(event: dict) -> None:
    """Buffer + flush one audit event. Never raises.

    Wire path:
      1. Sink disabled -> no-op (default).
      2. Sink enabled -> canonicalize, append to in-memory buffer,
         attempt flush to GitHub.
      3. Flush failure -> buffer retained, failure count incremented.
      4. After `_MAX_FAILURES_BEFORE_DROP` consecutive failures, drop
         the buffer with an error notification and reset the counter.
    """
    if not _sink_enabled():
        return

    global _FAILURE_COUNT
    canonical = _canonicalize(event)

    with _LOCK:
        _BUFFER.append(canonical)
        batch = list(_BUFFER)

        ok = _flush_to_github(batch)
        if ok:
            _BUFFER.clear()
            _FAILURE_COUNT = 0
            return

        _FAILURE_COUNT += 1
        if _FAILURE_COUNT >= _MAX_FAILURES_BEFORE_DROP:
            dropped = len(_BUFFER)
            _BUFFER.clear()
            _FAILURE_COUNT = 0
            _structured_warn(
                "audit_sink_buffer_dropped",
                dropped_event_count=dropped,
                consecutive_failures=_MAX_FAILURES_BEFORE_DROP,
            )
            _notify_failure(
                f"GitHub audit sink dropped {dropped} buffered event(s) after "
                f"{_MAX_FAILURES_BEFORE_DROP} consecutive failures. "
                f"Local audit log is unaffected."
            )


def _reset_state_for_tests() -> None:
    """Test-only convenience: clear buffer + failure counter."""
    global _FAILURE_COUNT
    with _LOCK:
        _BUFFER.clear()
        _FAILURE_COUNT = 0


# ── CLI: token issuance ──────────────────────────────────────────────────


def _sha256_hex(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _slug(actor_id: str) -> str:
    """Slugify an actor_id for use as a TOML table key.

    Replaces every non-alphanumeric character with `_`. Preserves
    case so `service-account-1` -> `service_account_1`.
    """
    out = []
    for ch in (actor_id or "").strip():
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out) or "anonymous"


def issue_token(
    actor_id: str,
    role: str,
    quota_usd: float,
    note: str = "",
    roles_toml_path: Optional[Path] = None,
) -> str:
    """Mint a fresh API token, append its hash to roles.toml, return plaintext.

    The plaintext token is returned ONCE. Callers print it with a "store
    this now" warning; it is never stored on disk.

    Audit event `api_key_issued` is logged via audit.log; the event
    carries the actor_id + role + quota in metadata but NEVER the
    plaintext token.
    """
    if not actor_id or not actor_id.strip():
        raise ValueError("actor_id is required")
    if role not in {"viewer", "contributor", "editor", "admin"}:
        raise ValueError(f"role must be one of viewer|contributor|editor|admin, got {role!r}")
    try:
        quota_f = float(quota_usd)
    except (TypeError, ValueError) as e:
        raise ValueError(f"quota_usd must be a number, got {quota_usd!r}") from e
    if quota_f < 0:
        raise ValueError("quota_usd must be >= 0 (0 means unlimited)")

    token = secrets.token_urlsafe(32)
    digest = _sha256_hex(token)
    path = roles_toml_path or _roles_toml_path()

    # Append a new [api_keys.<slug>] block to roles.toml. Use plain
    # text appending so the file stays human-editable; tomllib is
    # read-only and a full re-serialize would lose comments.
    slug = _slug(actor_id)
    block = (
        f"\n[api_keys.{slug}]\n"
        f'sha256          = "{digest}"\n'
        f'actor_id        = "{actor_id}"\n'
        f'role            = "{role}"\n'
        f"daily_quota_usd = {quota_f}\n"
        f'issued_at       = "{_utc_now_iso()}"\n'
    )
    if note:
        safe_note = note.replace('"', '\\"')
        block += f'note            = "{safe_note}"\n'

    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(block)
    except OSError as e:
        raise RuntimeError(f"could not write to {path}: {e}") from e

    # Audit event: actor_id + role + quota only. No plaintext token.
    try:
        import audit
        audit.log(
            actor_id,
            "api_key_issued",
            extra={
                "role": role,
                "daily_quota_usd": quota_f,
                "sha256_prefix": digest[:8],
                "issued_via": "cli",
            },
        )
    except Exception:
        # audit unavailable in some test envs; don't fail issuance.
        pass

    return token


def _cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m audit_github_sink",
        description="TF Tool Phase 21 token / audit-sink helper.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    issue = sub.add_parser("issue", help="Issue a new per-key API token.")
    issue.add_argument("--actor-id", required=True, help="Stable identifier for the caller.")
    issue.add_argument("--role", required=True, choices=["viewer", "contributor", "editor", "admin"])
    issue.add_argument("--quota-usd", required=True, type=float, help="Daily quota in USD. 0 means unlimited.")
    issue.add_argument("--note", default="", help="Optional human-readable description.")

    args = parser.parse_args(argv)

    if args.cmd == "issue":
        try:
            token = issue_token(args.actor_id, args.role, args.quota_usd, note=args.note)
        except (ValueError, RuntimeError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print("")
        print("=" * 60)
        print(f"  TOKEN ISSUED for actor_id={args.actor_id}")
        print("=" * 60)
        print(f"  {token}")
        print("=" * 60)
        print("  Store this token now. It will NOT be shown again.")
        print("  The hash has been appended to roles.toml.")
        print("=" * 60)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
