"""Tests for `gcp_client`. Standalone-runnable: `python tests/test_gcp_client.py`.

Mocks the google-cloud-* SDK clients so the suite runs without real GCP creds.
"""

from __future__ import annotations

import json
import os
import sys
import types
import unittest.mock as mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _install_fake_google_modules():
    """Install minimal stubs for google.cloud.* modules so gcp_client imports
    succeed even when the real SDKs are not installed in this venv. Each
    submodule exposes the client class names and types namespace that
    gcp_client touches."""

    def _module(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    # google.api_core.exceptions
    if "google.api_core.exceptions" not in sys.modules:
        api_core = _module("google.api_core")
        exc = _module("google.api_core.exceptions")

        class GoogleAPICallError(Exception):
            pass

        class NotFound(GoogleAPICallError):
            pass

        class PermissionDenied(GoogleAPICallError):
            pass

        exc.GoogleAPICallError = GoogleAPICallError
        exc.NotFound = NotFound
        exc.PermissionDenied = PermissionDenied
        api_core.exceptions = exc

    # google.auth.exceptions
    if "google.auth.exceptions" not in sys.modules:
        auth = _module("google.auth")
        auth_exc = _module("google.auth.exceptions")

        class DefaultCredentialsError(Exception):
            pass

        auth_exc.DefaultCredentialsError = DefaultCredentialsError
        auth.exceptions = auth_exc

    # google.oauth2.service_account
    if "google.oauth2" not in sys.modules:
        _module("google.oauth2")
    if "google.oauth2.service_account" not in sys.modules:
        sa = _module("google.oauth2.service_account")

        class Credentials:
            @staticmethod
            def from_service_account_info(info):
                if "client_email" not in info:
                    raise KeyError("client_email")
                obj = mock.MagicMock(name="Credentials")
                obj.service_account_email = info["client_email"]
                return obj

        sa.Credentials = Credentials

    # google.cloud.functions_v2 / iam_admin_v1 / pubsub_v1 / run_v2
    if "google.cloud" not in sys.modules:
        _module("google.cloud")
    for name, client_attr in [
        ("google.cloud.functions_v2", "FunctionServiceClient"),
        ("google.cloud.iam_admin_v1", "IAMClient"),
        ("google.cloud.pubsub_v1", "PublisherClient"),
        ("google.cloud.run_v2", "ServicesClient"),
    ]:
        if name not in sys.modules:
            mod = _module(name)
            setattr(mod, client_attr, mock.MagicMock(name=client_attr))

    # google.cloud.iam_admin_v1.types
    if "google.cloud.iam_admin_v1.types" not in sys.modules:
        types_mod = _module("google.cloud.iam_admin_v1.types")
        types_mod.ListServiceAccountsRequest = lambda **kw: kw
        types_mod.GetServiceAccountRequest = lambda **kw: kw
        sys.modules["google.cloud.iam_admin_v1"].types = types_mod


_install_fake_google_modules()

from gcp_client import GcpClient, GcpError, _build_credentials  # noqa: E402


VALID_SA_JSON = json.dumps({
    "type": "service_account",
    "project_id": "test-project",
    "private_key_id": "x",
    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
    "client_email": "test@test-project.iam.gserviceaccount.com",
    "client_id": "111",
    "token_uri": "https://oauth2.googleapis.com/token",
})


def test_build_credentials_returns_none_for_empty_input():
    assert _build_credentials("") is None


def test_build_credentials_rejects_non_json():
    try:
        _build_credentials("not json")
    except GcpError as e:
        assert "not valid JSON" in str(e)
        return
    raise AssertionError("expected GcpError")


def test_build_credentials_rejects_missing_client_email():
    bad = json.dumps({"type": "service_account"})
    try:
        _build_credentials(bad)
    except GcpError as e:
        assert "missing required" in str(e)
        return
    raise AssertionError("expected GcpError")


def test_build_credentials_succeeds_for_valid_sa_json():
    out = _build_credentials(VALID_SA_JSON)
    assert out is not None
    assert out.service_account_email == "test@test-project.iam.gserviceaccount.com"


def test_gcp_client_requires_project_id():
    try:
        GcpClient(project_id="", sa_json=VALID_SA_JSON)
    except GcpError as e:
        assert "project_id is required" in str(e)
        return
    raise AssertionError("expected GcpError")


def test_list_functions_returns_normalised_dicts():
    client = GcpClient(project_id="test-project", sa_json=VALID_SA_JSON)

    fake_fn = mock.MagicMock()
    fake_fn.name = "projects/test-project/locations/us-central1/functions/my-func"
    fake_fn.service_config.uri = "https://my-func-uri"
    fake_fn.build_config.runtime = "python311"
    client._functions = mock.MagicMock()
    client._functions.list_functions.return_value = [fake_fn]

    result = client.list_functions()
    assert len(result) == 1
    assert result[0]["name"] == "my-func"
    assert result[0]["full_name"].endswith("/my-func")
    assert result[0]["uri"] == "https://my-func-uri"
    assert result[0]["runtime"] == "python311"


def test_list_pubsub_topics_strips_project_prefix():
    client = GcpClient(project_id="test-project", sa_json=VALID_SA_JSON)

    fake_topic = mock.MagicMock()
    fake_topic.name = "projects/test-project/topics/demo-events"
    client._pubsub = mock.MagicMock()
    client._pubsub.list_topics.return_value = [fake_topic]

    result = client.list_pubsub_topics()
    assert result == [{
        "name": "demo-events",
        "full_name": "projects/test-project/topics/demo-events",
    }]


def test_get_function_by_name_returns_none_on_not_found():
    from google.api_core.exceptions import NotFound
    client = GcpClient(project_id="test-project", sa_json=VALID_SA_JSON)
    client._functions = mock.MagicMock()
    client._functions.get_function.side_effect = NotFound("not found")
    assert client.get_function_by_name("missing") is None


def test_permission_denied_surfaces_helpful_message():
    from google.api_core.exceptions import PermissionDenied
    client = GcpClient(project_id="test-project", sa_json=VALID_SA_JSON)
    client._functions = mock.MagicMock()
    client._functions.list_functions.side_effect = PermissionDenied("nope")
    try:
        client.list_functions()
    except GcpError as e:
        assert "permission denied" in str(e).lower()
        assert "viewer roles" in str(e)
        return
    raise AssertionError("expected GcpError")


_TESTS = [
    test_build_credentials_returns_none_for_empty_input,
    test_build_credentials_rejects_non_json,
    test_build_credentials_rejects_missing_client_email,
    test_build_credentials_succeeds_for_valid_sa_json,
    test_gcp_client_requires_project_id,
    test_list_functions_returns_normalised_dicts,
    test_list_pubsub_topics_strips_project_prefix,
    test_get_function_by_name_returns_none_on_not_found,
    test_permission_denied_surfaces_helpful_message,
]


def main() -> int:
    passes = 0
    failures: list[tuple[str, str]] = []
    for fn in _TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
            passes += 1
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}: {e}")
            failures.append((fn.__name__, str(e)))
        except Exception as e:
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
            failures.append((fn.__name__, f"{type(e).__name__}: {e}"))

    print()
    print(f"{passes}/{len(_TESTS)} passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
