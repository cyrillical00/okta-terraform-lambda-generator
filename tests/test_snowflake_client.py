"""Tests for `snowflake_client.SnowflakeClient`.

Standalone-runnable: `python tests/test_snowflake_client.py`.

Mocks `snowflake.connector.connect` and the private-key loader to avoid
hitting a real Snowflake account and to keep the test fixtures synthetic
low-entropy per the global memory rule on credential fixtures.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from snowflake_client import (
    SnowflakeClient,
    SnowflakeAuthError,
    SnowflakeError,
    SnowflakeParseError,
    SnowflakeServerError,
)


# Synthetic low-entropy PEM fixture per [[test-fixture-tokens-must-be-low-entropy]].
# Matches the PEM redact regex but uses repeated-char body so gitleaks /
# GitHub push-protection do not flag it as a real key.
_FAKE_PEM = "-----BEGIN PRIVATE KEY-----\n" + "a" * 200 + "\n-----END PRIVATE KEY-----"


def _make_client_with_mocked_connect() -> SnowflakeClient:
    """Build a SnowflakeClient that will not contact the network when
    list_* is called. The PEM load step is bypassed so the synthetic
    fixture never has to be a real ASN.1 structure."""
    return SnowflakeClient(
        account="xy12345.us-east-1",
        user="TF_TOOL_SERVICE",
        private_key=_FAKE_PEM,
        role="TF_TOOL_READER",
        warehouse="COMPUTE_WH",
    )


def _stub_cursor(rows: list[tuple], cols: list[str]) -> MagicMock:
    """Build a cursor mock returning the given rows and column names from
    fetchall() / description."""
    cur = MagicMock()
    cur.fetchall.return_value = rows
    # cursor.description is a list of 7-tuples; only [0] (name) is used.
    cur.description = [(c, None, None, None, None, None, None) for c in cols]
    cur.execute.return_value = None
    cur.close.return_value = None
    return cur


def _stub_conn(cursor: MagicMock) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.close.return_value = None
    return conn


def test_init_rejects_missing_fields():
    for missing in ("account", "user", "private_key", "role", "warehouse"):
        kwargs = {
            "account": "xy12345",
            "user": "U",
            "private_key": _FAKE_PEM,
            "role": "R",
            "warehouse": "W",
        }
        kwargs[missing] = ""
        try:
            SnowflakeClient(**kwargs)
        except SnowflakeError as e:
            assert "required" in str(e).lower(), f"unexpected msg for missing {missing}: {e}"
        else:
            raise AssertionError(f"expected SnowflakeError for missing {missing}")


def test_list_warehouses_success():
    client = _make_client_with_mocked_connect()
    cur = _stub_cursor(
        rows=[("ETL_WH", "STARTED", "MEDIUM"), ("REPORTING_WH", "SUSPENDED", "SMALL")],
        cols=["name", "state", "size"],
    )
    conn = _stub_conn(cur)
    with patch.object(client, "_connect", return_value=conn):
        result = client.list_warehouses()
    assert result == [
        {"name": "ETL_WH", "state": "STARTED", "size": "MEDIUM"},
        {"name": "REPORTING_WH", "state": "SUSPENDED", "size": "SMALL"},
    ]
    cur.execute.assert_called_once_with("SHOW WAREHOUSES")


def test_list_databases_success():
    client = _make_client_with_mocked_connect()
    cur = _stub_cursor(
        rows=[("ANALYTICS", "ACCOUNTADMIN"), ("RAW", "SYSADMIN")],
        cols=["name", "owner"],
    )
    conn = _stub_conn(cur)
    with patch.object(client, "_connect", return_value=conn):
        result = client.list_databases()
    assert result == [
        {"name": "ANALYTICS", "owner": "ACCOUNTADMIN"},
        {"name": "RAW", "owner": "SYSADMIN"},
    ]
    cur.execute.assert_called_once_with("SHOW DATABASES")


def test_list_roles_success():
    client = _make_client_with_mocked_connect()
    cur = _stub_cursor(
        rows=[("ACCOUNTADMIN", "ACCOUNTADMIN"), ("DATA_ENGINEER", "SYSADMIN")],
        cols=["name", "owner"],
    )
    conn = _stub_conn(cur)
    with patch.object(client, "_connect", return_value=conn):
        result = client.list_roles()
    assert result == [
        {"name": "ACCOUNTADMIN", "owner": "ACCOUNTADMIN"},
        {"name": "DATA_ENGINEER", "owner": "SYSADMIN"},
    ]
    cur.execute.assert_called_once_with("SHOW ROLES")


def test_list_users_success():
    client = _make_client_with_mocked_connect()
    cur = _stub_cursor(
        rows=[("AIRFLOW_RUNNER", "DATA_ENGINEER")],
        cols=["name", "default_role"],
    )
    conn = _stub_conn(cur)
    with patch.object(client, "_connect", return_value=conn):
        result = client.list_users()
    assert result == [{"name": "AIRFLOW_RUNNER", "default_role": "DATA_ENGINEER"}]
    cur.execute.assert_called_once_with("SHOW USERS")


def test_insufficient_privileges_on_show_users_raises_auth_error():
    """A read-only env-context role typically cannot SHOW USERS. The driver
    raises ProgrammingError with 'Insufficient privileges' in the message;
    the client surfaces that as SnowflakeAuthError so the env_context layer
    can downgrade to partial_errors."""
    # Build a fake ProgrammingError class to side-effect the execute call.
    import snowflake.connector  # type: ignore

    class FakeProgrammingError(snowflake.connector.errors.ProgrammingError):
        def __init__(self, msg):
            super().__init__(msg=msg)

    client = _make_client_with_mocked_connect()
    cur = MagicMock()
    cur.execute.side_effect = FakeProgrammingError("Insufficient privileges to operate on USERS")
    cur.close.return_value = None
    conn = _stub_conn(cur)
    with patch.object(client, "_connect", return_value=conn):
        try:
            client.list_users()
        except SnowflakeAuthError as e:
            assert "Insufficient privileges" in str(e)
        else:
            raise AssertionError("expected SnowflakeAuthError on insufficient privileges")


def test_operational_error_raises_server_error():
    """A network-level OperationalError from the driver must surface as
    SnowflakeServerError so env_context.fetch_snowflake_context can
    downgrade it to a partial error."""
    import snowflake.connector  # type: ignore

    class FakeOperationalError(snowflake.connector.errors.OperationalError):
        def __init__(self, msg):
            super().__init__(msg=msg)

    client = _make_client_with_mocked_connect()
    cur = MagicMock()
    cur.execute.side_effect = FakeOperationalError("Connection reset by peer")
    cur.close.return_value = None
    conn = _stub_conn(cur)
    with patch.object(client, "_connect", return_value=conn):
        try:
            client.list_warehouses()
        except SnowflakeServerError as e:
            assert "operational" in str(e).lower() or "Connection reset" in str(e)
        else:
            raise AssertionError("expected SnowflakeServerError on operational error")


def test_close_releases_connection_and_is_idempotent():
    """close() should be safe to call multiple times and should null out
    the underlying handle so a subsequent list_* call re-opens via
    _connect()."""
    client = _make_client_with_mocked_connect()
    fake_conn = MagicMock()
    fake_conn.close.return_value = None
    client._conn = fake_conn
    client.close()
    fake_conn.close.assert_called_once()
    assert client._conn is None
    # Second call: no-op, no exception.
    client.close()
    assert client._conn is None


def test_context_manager_calls_close():
    """The SnowflakeClient supports the `with` protocol; exiting the block
    must call close() so connections do not leak when an env-context fetch
    raises mid-flight."""
    client = _make_client_with_mocked_connect()
    fake_conn = MagicMock()
    fake_conn.close.return_value = None
    client._conn = fake_conn
    with client as c:
        assert c is client
    fake_conn.close.assert_called_once()
    assert client._conn is None


def test_load_private_key_der_rejects_garbage():
    """The PEM loader must raise SnowflakeAuthError on malformed input so
    a wrong-passphrase / corrupted-secret scenario surfaces cleanly to
    the env-context layer."""
    from snowflake_client import _load_private_key_der
    try:
        _load_private_key_der("not a PEM at all")
    except SnowflakeAuthError as e:
        assert "Private key" in str(e)
    else:
        raise AssertionError("expected SnowflakeAuthError on malformed PEM")


def test_empty_show_response_returns_empty_list():
    """A fresh Snowflake account with no user-created warehouses still
    returns SHOW WAREHOUSES = empty; the client should propagate that as
    an empty list, not raise."""
    client = _make_client_with_mocked_connect()
    cur = _stub_cursor(rows=[], cols=["name", "state"])
    conn = _stub_conn(cur)
    with patch.object(client, "_connect", return_value=conn):
        result = client.list_warehouses()
    assert result == []


if __name__ == "__main__":
    import traceback
    failures = []
    tests = [
        test_init_rejects_missing_fields,
        test_list_warehouses_success,
        test_list_databases_success,
        test_list_roles_success,
        test_list_users_success,
        test_insufficient_privileges_on_show_users_raises_auth_error,
        test_operational_error_raises_server_error,
        test_close_releases_connection_and_is_idempotent,
        test_context_manager_calls_close,
        test_load_private_key_der_rejects_garbage,
        test_empty_show_response_returns_empty_list,
    ]
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception:
            failures.append(t.__name__)
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
