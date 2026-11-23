"""Tests for the repo-aware gates in roles.py and the Terraform import gate.

Streamlit secrets are replaced by a dict, so nothing here needs a running
app. The GitHub fetch is stubbed.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import repo_context
import roles

SECRETS = {
    "roles": {
        "admin": ["admin@example.com"],
        "editor": ["editor@example.com"],
        "contributor": ["acme@example.com", "mapped@example.com", "Mixed.Case@Example.com"],
        "viewer": ["viewer@example.com"],
    },
    "github_usernames": {
        "mapped@example.com": "Mapped-GH",
        "mixed.case@example.com": "mixed-gh",
        "viewer@example.com": "viewer-gh",
    },
}


@pytest.fixture(autouse=True)
def _secrets():
    with patch.object(roles, "_read_secrets_table", side_effect=lambda name: dict(SECRETS.get(name, {}))):
        yield


# ─── contributor ownership is an explicit mapping ────────────────────────


def test_email_local_part_no_longer_grants_a_repo_owner():
    """acme@example.com must not be able to push to the acme organisation."""
    assert roles.can_push_to("acme@example.com", "acme/production-infra") is False
    assert roles.can_read_repo("acme@example.com", "acme/production-infra") is False


def test_unmapped_contributor_cannot_push_anywhere():
    for repo in ("acme/x", "example/x", "acme@example.com/x"):
        assert roles.can_push_to("acme@example.com", repo) is False


def test_mapped_contributor_can_use_only_their_own_repos():
    assert roles.can_push_to("mapped@example.com", "mapped-gh/terraform") is True
    assert roles.can_push_to("mapped@example.com", "MAPPED-GH/terraform") is True
    assert roles.can_push_to("mapped@example.com", "mapped/terraform") is False
    assert roles.can_push_to("mapped@example.com", "someone-else/terraform") is False


def test_mapping_lookup_ignores_email_case():
    assert roles.can_push_to("Mixed.Case@Example.com", "mixed-gh/repo") is True


def test_editors_and_admins_are_not_restricted():
    assert roles.can_push_to("editor@example.com", "any-org/any-repo") is True
    assert roles.can_push_to("admin@example.com", "any-org/any-repo") is True


def test_viewers_and_unknown_users_get_nothing_even_with_a_mapping():
    assert roles.can_push_to("viewer@example.com", "viewer-gh/repo") is False
    assert roles.can_read_repo("viewer@example.com", "viewer-gh/repo") is False
    assert roles.can_read_repo("stranger@example.org", "stranger/repo") is False


@pytest.mark.parametrize("email, repo", [("", "a/b"), ("editor@example.com", ""), ("editor@example.com", "no-slash")])
def test_malformed_input_is_denied(email, repo):
    assert roles.can_push_to(email, repo) is False
    assert roles.can_read_repo(email, repo) is False


# ─── Terraform import runs the gate before touching GitHub ───────────────


def test_import_is_refused_before_any_github_call():
    with patch.object(repo_context, "fetch_terraform_files") as fetch:
        for email, repo in [
            ("viewer@example.com", "any-org/private-infra"),
            ("stranger@example.org", "any-org/private-infra"),
            ("acme@example.com", "acme/private-infra"),
            ("mapped@example.com", "someone-else/private-infra"),
        ]:
            with pytest.raises(repo_context.RepoAccessDenied):
                repo_context.fetch_terraform_files_as(email, "server-token", repo, "terraform")
    fetch.assert_not_called()


def test_import_is_allowed_for_permitted_roles():
    with patch.object(repo_context, "fetch_terraform_files", return_value={"terraform/main.tf": "x"}) as fetch:
        assert repo_context.fetch_terraform_files_as(
            "editor@example.com", "server-token", " any-org/infra ", "terraform"
        ) == {"terraform/main.tf": "x"}
        repo_context.fetch_terraform_files_as("mapped@example.com", "server-token", "mapped-gh/infra", "")
    assert fetch.call_args_list[0].args == ("server-token", "any-org/infra", "terraform")
    assert fetch.call_count == 2


def test_denial_is_a_runtime_error_so_the_ui_error_path_handles_it():
    assert issubclass(repo_context.RepoAccessDenied, RuntimeError)
