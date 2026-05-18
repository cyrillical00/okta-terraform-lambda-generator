"""Snowflake env-context client.

Uses the official snowflake-connector-python driver because Snowflake
forces key-pair JWT authentication (password auth is rejected at apply
time as of November 2025). The connector handles JWT signing natively
and reads the private key as DER bytes derived from the PEM string.

Public API mirrors fleet_client.py + jamf_client.py:
- SnowflakeClient(account, user, private_key, role, warehouse, passphrase=None)
- list_warehouses() -> list[dict]   # SHOW WAREHOUSES
- list_databases() -> list[dict]    # SHOW DATABASES
- list_roles() -> list[dict]        # SHOW ROLES
- list_users() -> list[dict]        # SHOW USERS (requires MANAGE GRANTS
                                    #   or equivalent; may fail with an
                                    #   empty list + partial_error on
                                    #   read-only service roles)

Exception hierarchy mirrors the JAMF / Fleet clients so env_context.py
can catch SnowflakeError as the base type and downgrade per-endpoint
failures to partial_errors without aborting the whole context fetch.

Connection cleanup: SnowflakeClient.close() releases the underlying
connector handle. env_context.fetch_snowflake_context() always calls
close() in a finally block; individual list_* methods do not own the
connection lifecycle.
"""

from __future__ import annotations

from typing import Any, Optional


class SnowflakeError(Exception):
    """Base class for Snowflake client errors. env_context.fetch_snowflake_context
    catches this base type to record partial failures without aborting the
    whole context fetch."""
    pass


class SnowflakeAuthError(SnowflakeError):
    """Authentication failed. Most often a bad private key, the wrong
    passphrase on an encrypted key, an expired key, a role-restricted
    service user, or a malformed account identifier."""
    pass


class SnowflakeServerError(SnowflakeError):
    """Network failure, timeout, or 5xx-style error from the Snowflake
    backend. Transient by nature; retry might succeed."""
    pass


class SnowflakeParseError(SnowflakeError):
    """SHOW response decoded but the row shape did not match the documented
    Snowflake columns. Surface this so future API drift is visible."""
    pass


def _load_private_key_der(pem: str, passphrase: Optional[str] = None) -> bytes:
    """Convert a PEM-encoded private key (RSA, PKCS#8 or PKCS#1) into the
    DER bytes the snowflake-connector-python driver expects.

    Raises SnowflakeAuthError if the PEM cannot be decoded or the passphrase
    is wrong. Imports are lazy so unit tests can mock the connector path
    without forcing `cryptography` on every consumer.
    """
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend
    except ImportError as exc:
        raise SnowflakeError(f"cryptography library not available: {exc}") from exc

    if not pem:
        raise SnowflakeAuthError("Private key is empty.")

    pem_bytes = pem.encode("utf-8") if isinstance(pem, str) else pem
    password_bytes = passphrase.encode("utf-8") if passphrase else None

    try:
        key = serialization.load_pem_private_key(
            pem_bytes,
            password=password_bytes,
            backend=default_backend(),
        )
    except (ValueError, TypeError) as exc:
        raise SnowflakeAuthError(f"Private key could not be parsed: {exc}") from exc
    except Exception as exc:
        raise SnowflakeAuthError(f"Private key load failed: {exc}") from exc

    try:
        return key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    except Exception as exc:
        raise SnowflakeAuthError(f"Private key DER serialization failed: {exc}") from exc


class SnowflakeClient:
    """Key-pair JWT client. One instance per env-context fetch; the
    underlying connector connection is opened lazily on the first SHOW
    call and released by close().

    Snowflake's SHOW statements return tabular result sets; each row
    surfaces as a dict keyed by the column names the driver reports.
    The connector standardises column names to lowercase strings (e.g.
    `name`, `created_on`, `state`).
    """

    DEFAULT_TIMEOUT = 30  # seconds, login + per-query

    def __init__(
        self,
        account: str,
        user: str,
        private_key: str,
        role: str,
        warehouse: str,
        passphrase: Optional[str] = None,
    ):
        if not account:
            raise SnowflakeError("Snowflake account is required.")
        if not user:
            raise SnowflakeError("Snowflake user is required.")
        if not private_key:
            raise SnowflakeError("Snowflake private_key is required.")
        if not role:
            raise SnowflakeError("Snowflake role is required.")
        if not warehouse:
            raise SnowflakeError("Snowflake warehouse is required.")
        self.account = account
        self.user = user
        self._private_key_pem = private_key
        self._passphrase = passphrase
        self.role = role
        self.warehouse = warehouse
        self._conn = None

    def _connect(self):
        """Open the connector connection on first use. Reuses the same
        connection across subsequent SHOW calls in the same env-context
        fetch."""
        if self._conn is not None:
            return self._conn

        try:
            import snowflake.connector
        except ImportError as exc:
            raise SnowflakeError(
                f"snowflake-connector-python is not installed: {exc}. "
                f"Install with: pip install snowflake-connector-python>=3.10.0"
            ) from exc

        private_key_der = _load_private_key_der(self._private_key_pem, self._passphrase)

        try:
            self._conn = snowflake.connector.connect(
                account=self.account,
                user=self.user,
                private_key=private_key_der,
                role=self.role,
                warehouse=self.warehouse,
                login_timeout=self.DEFAULT_TIMEOUT,
                network_timeout=self.DEFAULT_TIMEOUT,
            )
        except snowflake.connector.errors.DatabaseError as exc:
            msg = str(exc)
            if "Incorrect username or password" in msg or "JWT token is invalid" in msg or "authentication" in msg.lower():
                raise SnowflakeAuthError(f"Snowflake authentication failed: {msg}") from exc
            raise SnowflakeServerError(f"Snowflake connect failed: {msg}") from exc
        except Exception as exc:
            raise SnowflakeServerError(f"Snowflake connect failed: {exc}") from exc

        return self._conn

    def close(self) -> None:
        """Release the underlying connector handle. Safe to call multiple
        times; subsequent SHOW calls will re-open a fresh connection."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                # The connection may already be torn down; swallow so callers
                # in finally blocks do not get a secondary exception.
                pass
            self._conn = None

    def _show(self, statement: str) -> list[dict]:
        """Run a SHOW statement and return each row as a dict keyed by
        the lowercased column names the driver reports.

        Raises SnowflakeAuthError on auth-class errors (privilege missing,
        role too restrictive), SnowflakeServerError on network / timeout,
        SnowflakeParseError on shape mismatch.
        """
        conn = self._connect()
        try:
            import snowflake.connector
        except ImportError as exc:
            raise SnowflakeError(f"snowflake-connector-python missing at SHOW time: {exc}") from exc

        cursor = conn.cursor()
        try:
            try:
                cursor.execute(statement)
            except snowflake.connector.errors.ProgrammingError as exc:
                msg = str(exc)
                if "Insufficient privileges" in msg or "does not exist or not authorized" in msg:
                    raise SnowflakeAuthError(f"Snowflake denied {statement}: {msg}") from exc
                raise SnowflakeServerError(f"Snowflake {statement} failed: {msg}") from exc
            except snowflake.connector.errors.OperationalError as exc:
                raise SnowflakeServerError(f"Snowflake {statement} operational error: {exc}") from exc
            except Exception as exc:
                raise SnowflakeServerError(f"Snowflake {statement} failed: {exc}") from exc

            try:
                rows = cursor.fetchall()
                desc = cursor.description or []
                col_names = [d[0].lower() if d and d[0] else f"col{i}" for i, d in enumerate(desc)]
            except Exception as exc:
                raise SnowflakeParseError(f"Snowflake {statement} fetchall failed: {exc}") from exc
        finally:
            try:
                cursor.close()
            except Exception:
                pass

        try:
            return [dict(zip(col_names, row)) for row in rows]
        except Exception as exc:
            raise SnowflakeParseError(f"Snowflake {statement} row mapping failed: {exc}") from exc

    def list_warehouses(self) -> list[dict]:
        """SHOW WAREHOUSES. Returns one dict per warehouse with at least
        a `name` key; other columns (state, size, owner, etc.) are also
        included verbatim from Snowflake's response."""
        return self._show("SHOW WAREHOUSES")

    def list_databases(self) -> list[dict]:
        """SHOW DATABASES. Returns one dict per database with at least a
        `name` key. Includes system databases (SNOWFLAKE, SNOWFLAKE_SAMPLE_DATA)
        and any shares the service role has access to."""
        return self._show("SHOW DATABASES")

    def list_roles(self) -> list[dict]:
        """SHOW ROLES. Returns one dict per role with at least a `name`
        key. The connecting service role itself appears in the list."""
        return self._show("SHOW ROLES")

    def list_users(self) -> list[dict]:
        """SHOW USERS. Returns one dict per user with at least a `name`
        key. Requires MANAGE GRANTS or USAGE on the user object; a
        read-only env-context role may receive an Insufficient privileges
        error which surfaces as SnowflakeAuthError."""
        return self._show("SHOW USERS")

    def __enter__(self) -> "SnowflakeClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
