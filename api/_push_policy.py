"""Server-side limits for `POST /api/push`.

The endpoint commits with the *server's* GitHub token, so the caller must
not be the one who decides where that token writes. This module holds the
checks the route runs before it touches GitHub:

  * the target repo must be on an operator-controlled allow-list
    (`TFGEN_PUSH_ALLOWED_REPOS`); with no list configured nothing is pushed
  * file paths must sit in the directories this tool generates into, with
    the file types it generates
  * the branch name must be well-formed; an empty branch gets a generated
    name instead of falling through to the repo's default branch

The default-branch refusal itself lives in `gh_push.push.push_to_github`
(`allow_default_branch=False`), because only that function knows what the
repo's default branch is.

Pure functions, no I/O apart from reading the environment.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

# Directories and file types produced by the generator (see _build_files in
# api/index.py, cli.py and app.py).
ALLOWED_DIRS = ("terraform", "lambda", "cloud_function", "fleet")
ALLOWED_SUFFIXES = (".tf", ".tfvars.example", ".py", ".txt", ".yml", ".yaml")

MAX_FILES = 50
MAX_PATH_CHARS = 200
MAX_TOTAL_BYTES = 1_000_000
MAX_COMMIT_MESSAGE_CHARS = 500

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_PATH_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_SLUG_RE = re.compile(r"[^A-Za-z0-9-]+")


class PushPolicyError(ValueError):
    """The request asks for something the push policy does not allow."""


def allowed_repos() -> frozenset[str]:
    """Lower-cased `owner/repo` names from TFGEN_PUSH_ALLOWED_REPOS."""
    raw = os.environ.get("TFGEN_PUSH_ALLOWED_REPOS") or ""
    return frozenset(
        r.strip().lower() for r in re.split(r"[,\s]+", raw) if r.strip()
    )


def check_repo(repo: str) -> str:
    """Return the repo name if it is well-formed and allow-listed."""
    name = (repo or "").strip()
    if not _REPO_RE.match(name) or ".." in name:
        raise PushPolicyError("repo must be in owner/repo form")
    if name.lower() not in allowed_repos():
        raise PushPolicyError("repository is not on the push allow-list")
    return name


def check_files(files: dict[str, str]) -> None:
    """Reject paths outside the generated directories and oversized pushes."""
    if not files:
        raise PushPolicyError("no files to push")
    if len(files) > MAX_FILES:
        raise PushPolicyError(f"too many files (limit {MAX_FILES})")
    total = 0
    for path, content in files.items():
        if not isinstance(path, str) or len(path) > MAX_PATH_CHARS or not _PATH_RE.match(path):
            raise PushPolicyError("file paths must be plain relative paths")
        parts = path.split("/")
        if ".." in parts or len(parts) < 2 or parts[0] not in ALLOWED_DIRS:
            raise PushPolicyError(
                "file paths must be under one of: " + ", ".join(f"{d}/" for d in ALLOWED_DIRS)
            )
        if not path.endswith(ALLOWED_SUFFIXES):
            raise PushPolicyError(
                "file type not allowed; expected one of: " + ", ".join(ALLOWED_SUFFIXES)
            )
        total += len((content or "").encode("utf-8"))
    if total > MAX_TOTAL_BYTES:
        raise PushPolicyError(f"push is too large (limit {MAX_TOTAL_BYTES} bytes)")


def resolve_branch(branch: str, actor_id: str) -> str:
    """Validate the requested branch, or generate one when none was given.

    An empty branch never means "the default branch" here.
    """
    name = (branch or "").strip()
    if not name:
        slug = _SLUG_RE.sub("-", actor_id or "").strip("-")[:40] or "api"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"tfgen/{slug}-{stamp}"
    if (
        not _BRANCH_RE.match(name)
        or ".." in name
        or "//" in name
        or name.endswith(("/", ".", ".lock"))
    ):
        raise PushPolicyError("branch name is not valid")
    return name


def check_commit_message(message: str) -> str:
    text = (message or "").strip()
    if not text:
        raise PushPolicyError("commit_message is required")
    if len(text) > MAX_COMMIT_MESSAGE_CHARS:
        raise PushPolicyError(f"commit_message is too long (limit {MAX_COMMIT_MESSAGE_CHARS} characters)")
    return text
