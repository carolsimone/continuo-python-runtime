"""DuckLakeSession over a fake connection: the redaction choke point, the
transaction protocol and the passfile. No DuckDB instance is opened."""
import logging
import os
import stat

import duckdb
import pytest

from continuo_duckdb_adapter.application.ports import LakeConflictError
from continuo_duckdb_adapter.domain.identifiers import Identifier, QualifiedTable
from continuo_duckdb_adapter.infrastructure.ddl import DdlRenderer, attach_statement
from continuo_duckdb_adapter.infrastructure.passfile import Passfile, can_store
from continuo_duckdb_adapter.infrastructure.session import DuckLakeSession
from continuo_duckdb_adapter.infrastructure.settings import DuckLakeSettings

PASSWORD = "p w'd\\x"
# raw, libpq-quoted, SQL-literal form of the libpq-quoted value, SQL-doubled raw
SPELLINGS = [PASSWORD, "'p w\\'d\\\\x'", "''p w\\''d\\\\x''", "p w''d\\x"]
S3_SECRET = "s3-Secret-Zq"
ENV = {
    "DUCKDB_CATALOG_HOST": "localhost", "DUCKDB_CATALOG_DB": "catalog",
    "DUCKDB_CATALOG_USER": "continuo", "DUCKDB_DATA_PATH": "s3://warehouse/lake/",
    "DUCKDB_CATALOG_PASSWORD": PASSWORD,
    "DUCKDB_S3_ACCESS_KEY_ID": "key", "DUCKDB_S3_SECRET_ACCESS_KEY": S3_SECRET,
}
T = QualifiedTable.of("s", "t")


class FakeConnection:
    """Records statements; raises the error queued for a statement prefix."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.errors: dict[str, Exception] = {}
        self.closed = False

    def execute(self, sql, parameters=None):
        self.statements.append(sql)
        for prefix, error in self.errors.items():
            if sql.startswith(prefix):
                raise error
        return self

    def register(self, name, data):
        self.statements.append(f"register {name}")
        for prefix, error in self.errors.items():
            if "register".startswith(prefix):
                raise error

    def unregister(self, name):
        self.statements.append(f"unregister {name}")

    def fetchone(self):
        return (1,)

    def to_arrow_table(self):
        return "arrow"

    def close(self):
        self.closed = True


def make_session(connection=None):
    settings = DuckLakeSettings.from_env(ENV)
    from continuo_duckdb_adapter.infrastructure.ddl import secret_texts

    connection = connection or FakeConnection()
    return DuckLakeSession(connection, DdlRenderer("lake"), secrets=secret_texts(settings)), connection


def leaky(kind, text):
    return kind(f'Unable to connect to Postgres at "host=h dbname=d password={text}": refused')


@pytest.mark.parametrize("spelling", SPELLINGS + [S3_SECRET])
def test_every_operation_redacts_every_spelling(spelling):
    operations = {
        "create_schema_if_not_exists": lambda s: s.create_schema_if_not_exists(Identifier("s")),
        "drop_schema_cascade": lambda s: s.drop_schema_cascade(Identifier("s")),
        "table_exists": lambda s: s.table_exists(T),
        "delete_all": lambda s: s.delete_all(T),
        "fetch_arrow": lambda s: s.fetch_arrow("SELECT 1"),
        "explain_read": lambda s: s.explain_read("SELECT 1"),
    }
    prefixes = {
        "create_schema_if_not_exists": "CREATE SCHEMA", "drop_schema_cascade": "DROP SCHEMA",
        "table_exists": "SELECT count", "delete_all": "DELETE", "fetch_arrow": "SELECT 1",
        "explain_read": "EXPLAIN",
    }
    for name, operation in operations.items():
        session, connection = make_session()
        connection.errors[prefixes[name]] = leaky(duckdb.IOException, spelling)
        with pytest.raises(duckdb.IOException) as caught:
            operation(session)
        assert spelling not in str(caught.value), name
        assert "***" in str(caught.value), name
        assert "host=h dbname=d" in str(caught.value), name
        assert caught.value.__cause__ is None and caught.value.__suppress_context__, name


def test_insert_arrow_and_begin_are_redacted_too():
    import pyarrow as pa

    session, connection = make_session()
    connection.errors["INSERT"] = leaky(duckdb.IOException, PASSWORD)
    with pytest.raises(duckdb.IOException) as caught:
        session.insert_arrow(T, pa.table({"a": [1]}))
    assert PASSWORD not in str(caught.value)
    session, connection = make_session()
    connection.errors["BEGIN"] = leaky(duckdb.IOException, PASSWORD)
    with pytest.raises(duckdb.IOException) as caught, session.transaction():
        pass
    assert PASSWORD not in str(caught.value)


def test_exception_type_is_preserved_with_a_duckdb_error_fallback():
    session, connection = make_session()
    connection.errors["DELETE"] = leaky(duckdb.CatalogException, PASSWORD)
    with pytest.raises(duckdb.CatalogException):
        session.delete_all(T)

    class Odd(duckdb.Error):
        def __init__(self, a, b):  # non-standard constructor
            super().__init__(f"{a}{b}")

    connection.errors["DELETE"] = Odd("password=", PASSWORD)
    with pytest.raises(duckdb.Error) as caught:
        session.delete_all(T)
    assert type(caught.value) is duckdb.Error
    assert PASSWORD not in str(caught.value)


def test_transaction_exception_maps_to_a_redacted_conflict():
    session, connection = make_session()
    connection.errors["DELETE"] = leaky(duckdb.TransactionException, PASSWORD)
    with pytest.raises(LakeConflictError) as caught:
        session.delete_all(T)
    assert PASSWORD not in str(caught.value) and "***" in str(caught.value)


def test_an_empty_secret_is_never_redacted():
    settings = DuckLakeSettings.from_env({**ENV, "DUCKDB_CATALOG_PASSWORD": ""})
    from continuo_duckdb_adapter.infrastructure.ddl import secret_texts

    connection = FakeConnection()
    session = DuckLakeSession(connection, DdlRenderer("lake"), secrets=(*secret_texts(settings), ""))
    connection.errors["DELETE"] = duckdb.IOException("plain message stays intact")
    with pytest.raises(duckdb.IOException, match="plain message stays intact"):
        session.delete_all(T)


def test_a_failed_commit_is_redacted_and_not_rolled_back():
    session, connection = make_session()
    connection.errors["COMMIT"] = leaky(duckdb.TransactionException, PASSWORD)
    with pytest.raises(LakeConflictError) as caught, session.transaction():
        pass
    assert PASSWORD not in str(caught.value)
    assert connection.statements == ["BEGIN", "COMMIT"]


def test_a_failing_body_rolls_back_exactly_once():
    session, connection = make_session()
    with pytest.raises(ValueError), session.transaction():
        raise ValueError("body")
    assert connection.statements == ["BEGIN", "ROLLBACK"]


def test_a_clean_body_commits_without_rollback():
    session, connection = make_session()
    with session.transaction():
        pass
    assert connection.statements == ["BEGIN", "COMMIT"]


def test_rollback_failure_is_logged_redacted_and_never_masks_the_body_error(caplog):
    session, connection = make_session()
    connection.errors["ROLLBACK"] = leaky(duckdb.IOException, PASSWORD)
    with caplog.at_level(logging.WARNING, logger="continuo_duckdb_adapter"):
        with pytest.raises(ValueError, match="body"), session.transaction():
            raise ValueError("body")
    messages = [r.getMessage() for r in caplog.records]
    assert any("rollback failed" in m for m in messages)
    assert all(PASSWORD not in m for m in messages)


def test_explain_read_rolls_back_even_when_the_bind_fails():
    session, connection = make_session()
    connection.errors["EXPLAIN"] = duckdb.BinderException("no such column")
    with pytest.raises(duckdb.BinderException):
        session.explain_read("SELECT nope")
    assert connection.statements[0] == "BEGIN"
    assert connection.statements[-1] == "ROLLBACK"


def test_attach_statement_with_a_passfile_carries_no_password():
    settings = DuckLakeSettings.from_env(ENV)
    statement = attach_statement(settings, "lake", "/tmp/continuo pg/pass")
    assert "passfile='/tmp/continuo pg/pass'" in statement.replace("''", "'")
    assert "password" not in statement and PASSWORD not in statement


def test_passfile_is_private_holds_an_escaped_entry_and_is_removed():
    passfile = Passfile("a:b\\c")
    try:
        assert stat.S_IMODE(os.stat(passfile.path).st_mode) == 0o600
        assert open(passfile.path).read() == "*:*:*:*:a\\:b\\\\c\n"
    finally:
        passfile.close()
    assert not os.path.exists(passfile.path)
    passfile.close()  # idempotent


@pytest.mark.parametrize("password,storable", [
    ("pw", True), ("", False), ("two\nlines", False), ("cr\rhere", False),
])
def test_can_store(password, storable):
    assert can_store(password) is storable


def test_close_closes_the_connection_and_removes_the_passfile():
    passfile = Passfile("pw")
    connection = FakeConnection()
    session = DuckLakeSession(connection, DdlRenderer("lake"), passfile=passfile)
    session.close()
    assert connection.closed and not os.path.exists(passfile.path)
