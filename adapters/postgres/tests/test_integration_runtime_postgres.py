"""Tier-2 live tests against postgres:16 (tests/smoke/postgres-stack/docker-compose.yml).

Run the stack first:
    docker compose -f tests/smoke/postgres-stack/docker-compose.yml up -d --wait
"""
import decimal
import os
import uuid

import psycopg2
import pyarrow as pa
import pytest

from continuo_python_runtime_postgres.adapter import PostgresAdapter

PG = dict(
    host="localhost",
    port=os.environ.get("VR_IT_PG_PORT", "15499"),
    dbname="warehouse",
    user="continuo",
    password="continuo",
)


def _conn():
    return psycopg2.connect(**PG)


def _adapter():
    return PostgresAdapter(_conn())


@pytest.fixture()
def clean_schema():
    """Drop a fresh, uniquely-named schema before and after each test."""
    schema = "runtime_it"
    conn = _conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    conn.close()
    yield schema
    conn = _conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    conn.close()


def _columns(schema: str, table: str) -> list[tuple]:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
            (schema, table),
        )
        cols = cur.fetchall()
    conn.close()
    return cols


def _count(schema: str, table: str) -> int:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM "{schema}"."{table}"')
        n = cur.fetchone()[0]
    conn.close()
    return n


def _seed(schema: str, table: str, ddl: str, rows_sql: str) -> None:
    conn = _conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        cur.execute(f'DROP TABLE IF EXISTS "{schema}"."{table}"')
        cur.execute(f'CREATE TABLE "{schema}"."{table}" ({ddl})')
        if rows_sql:
            cur.execute(rows_sql)
    conn.close()


@pytest.mark.integration
def test_ensure_table_creates_typed_table_with_not_null(clean_schema):
    """ensure_table creates a typed table; NOT NULL columns show up in the DDL."""
    a = _adapter()
    a.ensure_table(
        clean_schema,
        "typed",
        [
            {"name": "id", "type": "BIGINT", "nullable": False},
            {"name": "label", "type": "VARCHAR(10)", "nullable": True},
            {"name": "amount", "type": "NUMERIC(10,2)", "nullable": False},
        ],
        config={},
    )
    a.close()
    cols = _columns(clean_schema, "typed")
    assert cols == [
        ("id", "bigint", "NO"),
        ("label", "character varying", "YES"),
        ("amount", "numeric", "NO"),
    ]


@pytest.mark.integration
def test_ensure_table_is_idempotent(clean_schema):
    """Calling ensure_table twice for the same table does not fail."""
    a = _adapter()
    cols = [{"name": "id", "type": "INT", "nullable": True}]
    a.ensure_table(clean_schema, "again", cols, config={})
    a.ensure_table(clean_schema, "again", cols, config={})
    a.close()
    assert _columns(clean_schema, "again") == [("id", "integer", "YES")]


@pytest.mark.integration
def test_fetch_round_trips_rows_into_arrow(clean_schema):
    """fetch() maps postgres row values to the expected Arrow types."""
    _seed(
        clean_schema,
        "src",
        "id int, name text, amount numeric(10,2), d date, ok boolean",
        f'INSERT INTO "{clean_schema}"."src" VALUES '
        f"(1, 'a', 12.50, '2024-01-15', true), (2, 'b', 3.00, '2024-02-20', false)",
    )
    a = _adapter()
    table = a.fetch(f'SELECT id, name, amount, d, ok FROM "{clean_schema}"."src" ORDER BY id')
    a.close()

    assert table.num_rows == 2
    assert table.column("id").to_pylist() == [1, 2]
    assert table.column("id").type == "int64"
    assert table.column("name").to_pylist() == ["a", "b"]
    assert table.column("amount").to_pylist() == [decimal.Decimal("12.50"), decimal.Decimal("3.00")]
    assert str(table.column("amount").type).startswith("decimal128")
    assert str(table.column("d").type) == "date32[day]"
    assert table.column("ok").to_pylist() == [True, False]


@pytest.mark.integration
def test_load_replaces_contents_atomically(clean_schema):
    """load() truncates prior contents and inserts exactly the new rows."""
    _seed(
        clean_schema,
        "tgt",
        "id int, name text",
        f'INSERT INTO "{clean_schema}"."tgt" VALUES (9, \'junk1\'), (10, \'junk2\'), (11, \'junk3\')',
    )
    a = _adapter()
    data = pa.table({"id": pa.array([1, 2], type=pa.int64()), "name": pa.array(["x", "y"])})
    a.load(clean_schema, "tgt", data)
    a.close()

    assert _count(clean_schema, "tgt") == 2
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(f'SELECT id, name FROM "{clean_schema}"."tgt" ORDER BY id')
        rows = cur.fetchall()
    conn.close()
    assert rows == [(1, "x"), (2, "y")]


@pytest.mark.integration
def test_load_accepts_percent_in_quoted_identifier(clean_schema):
    """A percent sign in a quoted column name is data, not formatting syntax."""
    _seed(clean_schema, "rates", '"rate%" integer', "")
    a = _adapter()
    a.load(
        clean_schema,
        "rates",
        pa.table({"rate%": pa.array([7], type=pa.int32())}),
    )
    a.close()

    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(f'SELECT "rate%" FROM "{clean_schema}"."rates"')
        rows = cur.fetchall()
    conn.close()
    assert rows == [(7,)]


@pytest.mark.integration
def test_load_rollback_on_failure_leaves_prior_contents_intact(clean_schema):
    """A failing insert (VARCHAR overflow) rolls back the whole load, including the TRUNCATE."""
    _seed(
        clean_schema,
        "tgt2",
        "id int, label varchar(1)",
        f'INSERT INTO "{clean_schema}"."tgt2" VALUES (1, \'a\')',
    )
    a = _adapter()
    # "too-long" exceeds VARCHAR(1) and must fail the insert, rolling back the TRUNCATE too.
    data = pa.table({"id": pa.array([2], type=pa.int64()), "label": pa.array(["too-long"])})
    with pytest.raises(Exception):
        a.load(clean_schema, "tgt2", data)
    a.close()

    assert _count(clean_schema, "tgt2") == 1
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(f'SELECT id, label FROM "{clean_schema}"."tgt2"')
        rows = cur.fetchall()
    conn.close()
    assert rows == [(1, "a")]


@pytest.mark.integration
def test_failed_load_leaves_prior_contents_intact(clean_schema):
    """load is TRUNCATE + inserts in ONE explicit transaction: a mid-load
    failure must roll the TRUNCATE back, not leave an empty table.

    Distinct from test_load_rollback_on_failure_leaves_prior_contents_intact
    above (which fails on a VARCHAR overflow): this drives the failure
    through a NOT NULL violation, per the plan's atomicity test.
    """
    _seed(
        clean_schema,
        "tgt4",
        "id int not null, amount int",
        f'INSERT INTO "{clean_schema}"."tgt4" VALUES (1, 100), (2, 200)',
    )
    a = _adapter()
    bad = pa.table({
        "id": pa.array([None], type=pa.int64()),
        "amount": pa.array([None], type=pa.int64()),
    })
    with pytest.raises(Exception):
        a.load(clean_schema, "tgt4", bad)
    a.close()

    assert _count(clean_schema, "tgt4") == 2


@pytest.mark.integration
def test_load_zero_rows_just_truncates(clean_schema):
    """load() with a 0-row Arrow table truncates without attempting an insert."""
    _seed(
        clean_schema,
        "tgt3",
        "id int",
        f'INSERT INTO "{clean_schema}"."tgt3" VALUES (1), (2)',
    )
    a = _adapter()
    a.load(clean_schema, "tgt3", pa.table({"id": pa.array([], type=pa.int64())}))
    a.close()

    assert _count(clean_schema, "tgt3") == 0


@pytest.mark.integration
def test_fetch_rejects_duplicate_select_columns(clean_schema):
    """fetch() raises rather than silently keeping only the last of two same-named columns."""
    a = _adapter()
    with pytest.raises(ValueError, match="id"):
        a.fetch("SELECT 1 AS id, 2 AS id")
    a.close()


class _RewritingCreateSchemaCursor(psycopg2.extensions.cursor):
    """Live cursor that rewrites CREATE SCHEMA into a guaranteed server-side failure.

    Appends ``AUTHORIZATION nonexistent_role_xyz`` to reproduce a generic
    (non-Duplicate/UniqueViolation) CREATE SCHEMA failure against an otherwise-real
    connection. Every other statement — the advisory lock/unlock calls, commits,
    rollbacks — runs unmodified against the live database, so this exercises the
    adapter's real transaction handling.
    """

    def execute(self, query, vars=None):  # noqa: A002 - matches psycopg2's signature
        text = query.as_string(self) if hasattr(query, "as_string") else query
        if isinstance(text, str) and text.strip().upper().startswith("CREATE SCHEMA"):
            text = text.rstrip() + " AUTHORIZATION nonexistent_role_xyz"
        return super().execute(text, vars)


@pytest.mark.integration
def test_ensure_schema_generic_failure_rolls_back_and_releases_advisory_lock():
    """A non-Duplicate CREATE SCHEMA failure must not leak the session advisory lock.

    Before the fix, only DuplicateSchema/UniqueViolation triggered a rollback; any
    other CREATE SCHEMA error left the transaction aborted, so the finally block's
    pg_advisory_unlock raised InFailedSqlTransaction — masking the real error and
    leaking the lock (other callers on the same schema would hang forever).
    """
    schema = f"lock_leak_it_{uuid.uuid4().hex[:8]}"
    bad_conn = psycopg2.connect(cursor_factory=_RewritingCreateSchemaCursor, **PG)
    a = PostgresAdapter(bad_conn)

    with pytest.raises(psycopg2.Error):
        a.ensure_table(schema, "t", [{"name": "id", "type": "INT", "nullable": True}], config={})
    a.close()

    # Proof the session advisory lock was released: a second connection can
    # acquire it immediately (pg_try_advisory_lock does not block/wait).
    second = _conn()
    with second.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (schema,))
        acquired = cur.fetchone()[0]
        cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (schema,))
    second.commit()
    second.close()
    assert acquired is True
