"""RBAC for the TF Tool. Maps email -> role, enforces capability checks,
and ships sensible defaults so the tool works out of the box even without
a roles config.

Roles (most permissive to least):
  admin       — all features + can edit roles config + sees every user's audit/cost
  editor      — full generation/push, sees only their own audit/cost
  contributor — generation OK; push only to repos owned by them (literal username match)
  viewer      — parse + view; no generate, no push

Configuration: a `[roles]` table in Streamlit secrets:
    [roles]
    admin   = ["alice@example.com", "ops@example.com"]
    editor  = ["bob@example.com"]
    viewer  = ["intern@example.com"]
    default = "viewer"   # role for any signed-in email not listed above

Per-role daily $ quota: a `[quotas]` table:
    [quotas]
    admin       = 0      # unlimited (0 means no cap)
    editor      = 5.00
    contributor = 2.00
    viewer      = 0.50

If neither table exists in secrets, defaults below apply.

Module load is side-effect free; reads Streamlit secrets lazily.
"""

from __future__ import annotations

# Permissive ordering: index = privilege rank (higher = more privileged).
ROLE_ORDER = ["viewer", "contributor", "editor", "admin"]

# Default quota per role in USD/day. 0 means unlimited.
DEFAULT_QUOTAS = {
    "admin": 0.0,
    "editor": 5.00,
    "contributor": 2.00,
    "viewer": 0.50,
}

# Default role for emails not explicitly mapped. Tightest sensible default.
DEFAULT_FALLBACK_ROLE = "viewer"

# Capability matrix: action -> minimum role required.
_CAPS = {
    "parse": "viewer",
    "self_check": "viewer",
    "view_outputs": "viewer",
    "generate": "contributor",
    "regenerate": "contributor",
    "fix_issues": "contributor",
    "push_personal": "contributor",   # push to a repo owned by the actor
    "push_org": "editor",             # push to a repo owned by anyone else
    "env_refresh": "viewer",
    "manage_roles": "admin",
    "view_all_audit": "admin",
}


def _role_rank(role: str) -> int:
    try:
        return ROLE_ORDER.index(role)
    except ValueError:
        return -1


def _read_secrets_table(name: str) -> dict:
    """Lazy + defensive read of a Streamlit secrets table. Returns {} when
    streamlit is unavailable or the table is missing."""
    try:
        import streamlit as st
        try:
            tbl = st.secrets[name]
        except Exception:
            return {}
        out = {}
        try:
            for k in tbl.keys():
                out[k] = tbl[k]
        except Exception:
            return {}
        return out
    except Exception:
        return {}


def _email_to_role_map() -> dict:
    raw = _read_secrets_table("roles")
    out: dict[str, str] = {}
    for role, emails in raw.items():
        if role == "default":
            continue
        if role not in ROLE_ORDER:
            continue
        if not isinstance(emails, (list, tuple)):
            continue
        for e in emails:
            if isinstance(e, str) and e.strip():
                out[e.strip().lower()] = role
    return out


def _fallback_role() -> str:
    raw = _read_secrets_table("roles")
    val = raw.get("default") if isinstance(raw.get("default"), str) else None
    if val and val in ROLE_ORDER:
        return val
    return DEFAULT_FALLBACK_ROLE


def get_role(email: str) -> str:
    """Return the role for a given email. Falls back to the configured
    default role (or 'viewer' if none configured)."""
    if not email:
        return _fallback_role()
    return _email_to_role_map().get(email.strip().lower(), _fallback_role())


def can(action: str, role_or_email: str) -> bool:
    """Check whether the given role (or the role of the given email) can
    perform `action`. Unknown action -> deny."""
    required = _CAPS.get(action)
    if required is None:
        return False
    actor_role = role_or_email if role_or_email in ROLE_ORDER else get_role(role_or_email)
    return _role_rank(actor_role) >= _role_rank(required)


def can_push_to(email: str, repo: str) -> bool:
    """Repo-aware push gate. `repo` is "owner/name". Contributor can push
    only when owner == their GitHub username (derived from email local-part
    as a best-effort fallback when no explicit GitHub username is mapped)."""
    if not email or not repo or "/" not in repo:
        return False
    role = get_role(email)
    if _role_rank(role) >= _role_rank("editor"):
        return True
    if _role_rank(role) >= _role_rank("contributor"):
        owner = repo.split("/", 1)[0].strip().lower()
        # Best-effort: match the email local-part. If you want strict
        # mapping, add a [github_usernames] table to secrets and look it up
        # here. For now this is the common case (cyrillical00@gmail.com ->
        # cyrillical00).
        local = email.split("@", 1)[0].strip().lower()
        return owner == local
    return False


def daily_quota_usd(email: str) -> float:
    """Return the configured daily cap in USD for the given user. 0 means
    unlimited (admin default)."""
    role = get_role(email)
    raw = _read_secrets_table("quotas")
    val = raw.get(role)
    try:
        if val is not None:
            return max(0.0, float(val))
    except (TypeError, ValueError):
        pass
    return DEFAULT_QUOTAS.get(role, 0.50)


def quota_remaining(email: str, today_usd: float) -> float:
    """Compute the remaining budget for the day. Returns float('inf') for
    unlimited; otherwise max(0, cap - spent)."""
    cap = daily_quota_usd(email)
    if cap == 0:
        return float("inf")
    return max(0.0, cap - max(0.0, today_usd))


def is_quota_exhausted(email: str, today_usd: float) -> bool:
    """True if the user has used 100% (or more) of their daily cap."""
    cap = daily_quota_usd(email)
    if cap == 0:
        return False
    return today_usd >= cap
