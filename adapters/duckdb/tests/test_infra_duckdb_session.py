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
    # read-only: the bind check can never write, whatever the read contains
    assert connection.statements[0] == "BEGIN TRANSACTION READ ONLY"
    assert connection.statements[-1] == "ROLLBACK"


def test_connect_applies_the_temp_directory_only_when_set(connect_with):
    script, _, connect = connect_with([None], env={**ENV, "DUCKDB_TEMP_DIRECTORY": "/tmp/spill"})
    connect().close()
    assert "SET temp_directory = '/tmp/spill'" in script.connections[0].statements
    script, _, connect = connect_with([None])
    connect().close()
    assert not any("temp_directory" in s for s in script.connections[0].statements)


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


# --- the first-ATTACH initialisation race -----------------------------------

RACE = (
    'Failed to initialize DuckLake: Failed to execute query "CREATE TABLE '
    '"public"."ducklake_metadata"(...)": ERROR:  duplicate key value violates '
    'unique constraint "pg_type_typname_nsp_index"'
)


class ConnectScript:
    """duckdb.connect() replacement: hands out fake connections whose ATTACH
    raises the next scripted error (None = succeed)."""

    def __init__(self, attach_errors):
        self.attach_errors = list(attach_errors)
        self.connections: list[FakeConnection] = []

    def __call__(self):
        connection = FakeConnection()
        error = self.attach_errors.pop(0) if self.attach_errors else None
        if error is not None:
            connection.errors["ATTACH"] = error
        self.connections.append(connection)
        return connection


@pytest.fixture
def connect_with(monkeypatch):
    def run(attach_errors, env=ENV):
        script = ConnectScript(attach_errors)
        monkeypatch.setattr("continuo_duckdb_adapter.infrastructure.session.duckdb.connect", script)
        sleeps: list[float] = []
        settings = DuckLakeSettings.from_env({**env, "DUCKDB_EXTENSION_DIRECTORY": "/ext"})
        return script, sleeps, lambda: DuckLakeSession.connect(settings, sleep=sleeps.append)

    return run


def test_the_init_race_is_retried_on_a_fresh_connection(connect_with):
    script, sleeps, connect = connect_with([duckdb.Error(RACE), duckdb.Error(RACE), None])
    session = connect()
    assert len(script.connections) == 3
    assert [c.closed for c in script.connections] == [True, True, False]
    assert len(sleeps) == 2 and sleeps == sorted(sleeps) and all(s > 0 for s in sleeps)
    session.close()


def test_a_persistent_init_race_surfaces_after_bounded_attempts(connect_with):
    from continuo_duckdb_adapter.infrastructure import session as session_module

    script, _, connect = connect_with([duckdb.Error(RACE)] * 50)
    with pytest.raises(duckdb.Error, match="Failed to initialize DuckLake"):
        connect()
    assert len(script.connections) == session_module._ATTACH_ATTEMPTS > 1
    assert all(c.closed for c in script.connections)


@pytest.mark.parametrize("error", [
    duckdb.IOException('Unable to connect to Postgres at "host=h": Connection refused'),
    duckdb.IOException('Failed to attach DuckLake: FATAL:  password authentication failed'),
    duckdb.Error("Failed to initialize DuckLake: disk full"),
    duckdb.CatalogException("something else"),
])
def test_every_other_attach_error_is_not_retried(connect_with, error):
    script, sleeps, connect = connect_with([error])
    with pytest.raises(type(error)):
        connect()
    assert len(script.connections) == 1 and sleeps == []


def test_a_retried_race_error_is_still_redacted(connect_with):
    from continuo_duckdb_adapter.infrastructure import session as session_module

    leaking = duckdb.Error(f"{RACE} password={PASSWORD}")
    _, _, connect = connect_with([leaking] * session_module._ATTACH_ATTEMPTS)
    with pytest.raises(duckdb.Error) as caught:
        connect()
    assert PASSWORD not in str(caught.value)


def test_a_failed_connect_removes_the_passfile(connect_with, monkeypatch):
    made: list[Passfile] = []

    def recording(password):
        made.append(Passfile(password))
        return made[-1]

    monkeypatch.setattr("continuo_duckdb_adapter.infrastructure.session.Passfile", recording)
    _, _, connect = connect_with([duckdb.IOException("Connection refused")])
    with pytest.raises(duckdb.IOException):
        connect()
    assert made and not any(os.path.exists(p.path) for p in made)
