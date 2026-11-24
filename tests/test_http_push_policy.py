"""Tests for the limits on POST /api/push and for API-key role enforcement.

The endpoint commits with the server's GitHub token, so these check that a
key holder cannot pick an arbitrary repo, branch or path, and that the role
stored with the key is honoured. No network: push_to_github and the GitHub
client are stubbed.
"""

from __future__ import annotations

import hashlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("TFGEN_API_KEY", "test-secret-do-not-ship")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-fake")

from fastapi.testclient import TestClient

from api import _push_policy
from api.index import app
from headless_rate_limit import HTTP_RATE_LIMITER

LEGACY = {"X-API-Key": os.environ["TFGEN_API_KEY"]}
FAKE_URL = "https://github.com/owner/repo/commit/abcdef"
GOOD_FILES = {"terraform/okta.tf": 'resource "okta_group" "x" { name = "X" }'}


def _body(**overrides):
    body = {
        "files": dict(GOOD_FILES),
        "repo": "owner/repo",
        "branch": "feature/test",
        "commit_message": "feat: test push",
    }
    body.update(overrides)
    return body


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_for_test")
    monkeypatch.setenv("TFGEN_PUSH_ALLOWED_REPOS", "owner/repo, Other-Org/Infra")
    monkeypatch.delenv("TFGEN_LEGACY_KEY_ROLE", raising=False)
    monkeypatch.delenv("TFGEN_ROLES_TOML", raising=False)
    HTTP_RATE_LIMITER.reset()
    with patch("api.index.audit.log"):
        yield TestClient(app)
    HTTP_RATE_LIMITER.reset()


def _keys_file(tmp_path, monkeypatch, role: str) -> dict:
    """Write a roles.toml with one per-key token of the given role."""
    token = f"unit-test-token-{role}"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    path = tmp_path / "roles.toml"
    path.write_text(
        "[api_keys.k]\n"
        f'sha256 = "{digest}"\n'
        f'actor_id = "actor-{role}"\n'
        f'role = "{role}"\n'
        "daily_quota_usd = 1.0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TFGEN_ROLES_TOML", str(path))
    return {"X-API-Key": token}


# ─── repo allow-list ─────────────────────────────────────────────────────


def test_allowed_repo_is_pushed_with_default_branch_protection(client):
    with patch("api.index.push_to_github", return_value=FAKE_URL) as push:
        r = client.post("/api/push", headers=LEGACY, json=_body())
    assert r.status_code == 200, r.text
    assert r.json() == {"commit_url": FAKE_URL}
    args, kwargs = push.call_args
    assert args[1] == "owner/repo"
    assert kwargs == {"branch": "feature/test", "allow_default_branch": False}


def test_allow_list_match_is_case_insensitive(client):
    with patch("api.index.push_to_github", return_value=FAKE_URL):
        r = client.post("/api/push", headers=LEGACY, json=_body(repo="other-org/infra"))
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("repo", [
    "victim-org/production-infra",
    "owner/repo-two",
    "owner/repo/extra",
    "owner/../other",
    "owner",
    "https://github.com/owner/repo",
    "",
])
def test_repo_outside_the_allow_list_is_refused(client, repo):
    with patch("api.index.push_to_github") as push:
        r = client.post("/api/push", headers=LEGACY, json=_body(repo=repo))
    assert r.status_code in (400, 403), r.text
    push.assert_not_called()


def test_empty_allow_list_disables_push(client, monkeypatch):
    monkeypatch.delenv("TFGEN_PUSH_ALLOWED_REPOS")
    with patch("api.index.push_to_github") as push:
        r = client.post("/api/push", headers=LEGACY, json=_body())
    assert r.status_code == 503, r.text
    push.assert_not_called()


# ─── paths ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [
    ".github/workflows/x.yml",
    "terraform/../.github/workflows/x.yml",
    "terraform/.hidden/x.tf",
    "/terraform/okta.tf",
    "terraform//okta.tf",
    "terraform\\okta.tf",
    "okta.tf",
    "README.md",
    "scripts/deploy.sh",
    "terraform/run.sh",
    "terraform/okta.tf ",
    "lambda/",
    "t" * 250 + ".tf",
])
def test_paths_outside_the_generated_directories_are_refused(client, path):
    with patch("api.index.push_to_github") as push:
        r = client.post("/api/push", headers=LEGACY, json=_body(files={path: "x"}))
    assert r.status_code == 400, f"{path!r}: {r.text}"
    push.assert_not_called()


@pytest.mark.parametrize("path", [
    "terraform/okta.tf",
    "terraform/engineering_lambda.tf",
    "terraform/terraform.tfvars.example",
    "lambda/lambda_function.py",
    "lambda/requirements.txt",
    "cloud_function/main.py",
    "fleet/default.yml",
])
def test_generated_paths_are_accepted(client, path):
    with patch("api.index.push_to_github", return_value=FAKE_URL):
        r = client.post("/api/push", headers=LEGACY, json=_body(files={path: "x"}))
    assert r.status_code == 200, f"{path!r}: {r.text}"


def test_one_bad_path_rejects_the_whole_push(client):
    files = {**GOOD_FILES, ".github/workflows/x.yml": "on: push"}
    with patch("api.index.push_to_github") as push:
        r = client.post("/api/push", headers=LEGACY, json=_body(files=files))
    assert r.status_code == 400
    push.assert_not_called()


def test_size_limits(client):
    too_many = {f"terraform/f{i}.tf": "x" for i in range(_push_policy.MAX_FILES + 1)}
    too_big = {"terraform/big.tf": "x" * (_push_policy.MAX_TOTAL_BYTES + 1)}
    with patch("api.index.push_to_github") as push:
        assert client.post("/api/push", headers=LEGACY, json=_body(files=too_many)).status_code == 400
        assert client.post("/api/push", headers=LEGACY, json=_body(files=too_big)).status_code == 400
    push.assert_not_called()


# ─── branch ──────────────────────────────────────────────────────────────


def test_empty_branch_gets_a_generated_name_not_the_default_branch(client):
    with patch("api.index.push_to_github", return_value=FAKE_URL) as push:
        r = client.post("/api/push", headers=LEGACY, json=_body(branch=""))
    assert r.status_code == 200, r.text
    branch = push.call_args.kwargs["branch"]
    assert branch.startswith("tfgen/legacy-tfgen-"), branch
    assert push.call_args.kwargs["allow_default_branch"] is False


@pytest.mark.parametrize("branch", [
    "refs/heads/../main", "a..b", "feature//x", "-x", "/main", "main/", "x.lock",
    "has space", "x~1", "x^", "x:y", "x@{1}", "b" * 150,
])
def test_malformed_branch_names_are_refused(client, branch):
    with patch("api.index.push_to_github") as push:
        r = client.post("/api/push", headers=LEGACY, json=_body(branch=branch))
    assert r.status_code == 400, f"{branch!r}: {r.text}"
    push.assert_not_called()


def _fake_github(default_branch="main"):
    repo = MagicMock()
    repo.default_branch = default_branch
    gh = MagicMock()
    gh.get_repo.return_value = repo
    return gh, repo


@pytest.mark.parametrize("branch", ["main", ""])
def test_push_helper_refuses_the_default_branch_when_asked(branch):
    from gh_push.push import push_to_github
    gh, repo = _fake_github("main")
    with patch("gh_push.push.Github", return_value=gh):
        with pytest.raises(RuntimeError, match="default branch"):
            push_to_github(GOOD_FILES, "owner/repo", "t", "msg", branch=branch,
                           allow_default_branch=False)
    repo.create_git_tree.assert_not_called()
    repo.create_git_commit.assert_not_called()
    repo.get_git_ref.assert_not_called()


def test_push_helper_keeps_old_behaviour_for_other_callers():
    """CLI / Streamlit callers (their own gating) can still target the default branch."""
    from gh_push.push import push_to_github
    gh, repo = _fake_github("main")
    repo.create_git_commit.return_value.html_url = FAKE_URL
    with patch("gh_push.push.Github", return_value=gh):
        assert push_to_github(GOOD_FILES, "owner/repo", "t", "msg") == FAKE_URL
    repo.get_git_ref.assert_called_once_with("heads/main")


def test_default_branch_refusal_reaches_the_caller_as_400(client):
    gh, repo = _fake_github("main")
    with patch("gh_push.push.Github", return_value=gh):
        r = client.post("/api/push", headers=LEGACY, json=_body(branch="main"))
    assert r.status_code == 400, r.text
    assert "default branch" in r.json()["detail"]
    repo.create_git_commit.assert_not_called()


# ─── roles ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role, expected", [
    ("viewer", 403), ("contributor", 403), ("editor", 200), ("admin", 200),
    ("editr", 403),  # a typo in roles.toml grants nothing
])
def test_push_requires_editor_or_above(client, tmp_path, monkeypatch, role, expected):
    headers = _keys_file(tmp_path, monkeypatch, role)
    with patch("api.index.push_to_github", return_value=FAKE_URL) as push:
        r = client.post("/api/push", headers=headers, json=_body())
    assert r.status_code == expected, r.text
    assert push.called is (expected == 200)


@pytest.mark.parametrize("role, expected", [("viewer", 403), ("contributor", 200)])
def test_generate_requires_contributor_or_above(client, tmp_path, monkeypatch, role, expected):
    from core.service import GenerateResult
    headers = _keys_file(tmp_path, monkeypatch, role)
    canned = GenerateResult(intent={"resource_type": "okta_group"}, outputs={}, validation_result={})
    with patch("api.index.core_service.generate", return_value=canned) as gen, \
         patch("api._bootstrap.cost.wrap_client", side_effect=lambda c, _: c), \
         patch("api._bootstrap.cost.today_usd", return_value=0.0), \
         patch("api.index.cost.today_usd", return_value=0.0), \
         patch("api._bootstrap.audit.log"):
        r = client.post("/api/generate", headers=headers, json={"prompt": "create a group"})
    assert r.status_code == expected, r.text
    assert gen.called is (expected == 200)


def test_legacy_key_role_can_be_lowered(client, monkeypatch):
    monkeypatch.setenv("TFGEN_LEGACY_KEY_ROLE", "viewer")
    with patch("api.index.push_to_github") as push:
        r = client.post("/api/push", headers=LEGACY, json=_body())
    assert r.status_code == 403
    push.assert_not_called()


def test_unknown_legacy_role_falls_back_to_least_privilege(client, monkeypatch):
    monkeypatch.setenv("TFGEN_LEGACY_KEY_ROLE", "superuser")
    with patch("api.index.push_to_github") as push:
        r = client.post("/api/push", headers=LEGACY, json=_body())
    assert r.status_code == 403
    push.assert_not_called()


# ─── rate limit and errors ───────────────────────────────────────────────


def test_push_is_rate_limited(client):
    with patch("api.index.push_to_github", return_value=FAKE_URL):
        codes = [client.post("/api/push", headers=LEGACY, json=_body()).status_code for _ in range(32)]
    assert codes[:30] == [200] * 30
    assert codes[30:] == [429, 429]


def test_unexpected_push_error_is_not_echoed(client):
    boom = Exception("401 Bad credentials for token ghp_fake_for_test at api.github.com")
    with patch("api.index.push_to_github", side_effect=boom):
        r = client.post("/api/push", headers=LEGACY, json=_body())
    assert r.status_code == 500
    assert r.json() == {"detail": "push failed"}
