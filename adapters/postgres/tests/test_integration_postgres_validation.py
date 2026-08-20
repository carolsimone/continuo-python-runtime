"""Tier-2 live tests against postgres:16 (docker-compose.integration.yml)."""
import concurrent.futures
import os
import uuid

import psycopg2
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


@pytest.fixture()
def prod_table():
    """Create prod_it.src_table with two rows; drop candidate leftovers."""
    conn = _conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS prod_it")
        cur.execute("DROP TABLE IF EXISTS prod_it.src_table")
        cur.execute("CREATE TABLE prod_it.src_table (id int, name text)")
        cur.execute("INSERT INTO prod_it.src_table VALUES (1, 'a'), (2, 'b')")
    yield "prod_it"
    conn.close()


def _columns(schema: str, table: str) -> list[tuple[str, str]]:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
            (schema, table),
        )
        cols = cur.fetchall()
    conn.close()
    return cols


def _indexes(schema: str, table: str) -> list[tuple[str, str]]:
    """Return (indexname, indexdef) for *schema.table*, ordered by name."""
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname=%s AND tablename=%s ORDER BY indexname",
            (schema, table),
        )
        rows = cur.fetchall()
    conn.close()
    return rows


def _count(schema: str, table: str) -> int:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM "{schema}"."{table}"')
        n = cur.fetchone()[0]
    conn.close()
    return n


def _schema_exists(schema: str) -> bool:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name=%s", (schema,))
        found = cur.fetchone() is not None
    conn.close()
    return found


@pytest.mark.integration
def test_build_empty_from_sql_live(prod_table):
    """Build an empty candidate table shaped by a compiled SELECT."""
    a = PostgresAdapter(_conn())
    a.ensure_schema("_candidate_it")
    a.build_empty_from_sql("_candidate_it", "built", "SELECT id, name FROM prod_it.src_table")
    a.close()
    assert _columns("_candidate_it", "built") == [("id", "integer"), ("name", "text")]
    assert _count("_candidate_it", "built") == 0


@pytest.mark.integration
def test_clone_empty_from_prod_live(prod_table):
    """Clone an empty candidate table shaped like the prod table."""
    a = PostgresAdapter(_conn())
    a.ensure_schema("_candidate_it")
    a.clone_empty_from_prod("_candidate_it", "prod_it", "src_table")
    a.close()
    assert _columns("_candidate_it", "src_table") == [("id", "integer"), ("name", "text")]
    assert _count("_candidate_it", "src_table") == 0


@pytest.mark.integration
def test_build_is_rerun_idempotent(prod_table):
    """Building the same table twice drops and recreates instead of failing."""
    a = PostgresAdapter(_conn())
    a.ensure_schema("_candidate_it")
    for _ in range(2):  # second run must drop-and-recreate, not fail
        a.build_empty_from_sql("_candidate_it", "rerun", "SELECT id FROM prod_it.src_table")
    a.close()
    assert _count("_candidate_it", "rerun") == 0


@pytest.mark.integration
def test_drop_schema_removes_it_and_is_idempotent(prod_table):
    """drop_schema deletes the schema and its tables; dropping again is a no-op."""
    schema = f"_candidate_drop_{uuid.uuid4().hex[:8]}"
    a = PostgresAdapter(_conn())
    a.ensure_schema(schema)
    a.build_empty_from_sql(schema, "t", "SELECT id FROM prod_it.src_table")
    assert _schema_exists(schema)
    a.drop_schema(schema)
    assert not _schema_exists(schema)
    a.drop_schema(schema)  # absent schema → no error
    a.close()
    assert not _schema_exists(schema)


@pytest.mark.integration
def test_ensure_schema_race_all_callers_succeed():
    """Concurrent ensure_schema callers on the same schema all succeed."""
    schema = f"_candidate_race_{uuid.uuid4().hex[:8]}"

    def _one() -> bool:
        a = PostgresAdapter(_conn())
        a.ensure_schema(schema)
        a.close()
        return True

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        assert all(f.result() for f in [ex.submit(_one) for _ in range(8)])


@pytest.mark.integration
def test_build_empty_from_columns_creates_typed_empty_table():
    """Typed CREATE TABLE carries name, NOT NULL, and VARCHAR length into the DDL."""
    a = PostgresAdapter(_conn())
    a.ensure_schema("it_cols")
    a.build_empty_from_columns("it_cols", "t", [
        {"name": "id", "type": "INTEGER", "nullable": False},
        {"name": "email", "type": "VARCHAR(255)"},
        {"name": "amount", "type": "NUMERIC(10,2)"},
    ], {})
    a.close()
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable, character_maximum_length "
            "FROM information_schema.columns "
            "WHERE table_schema='it_cols' AND table_name='t' ORDER BY ordinal_position"
        )
        rows = cur.fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["id", "email", "amount"]
    assert rows[0][2] == "NO"            # NOT NULL honored
    assert rows[1][3] == 255             # VARCHAR length carried into DDL
    assert _count("it_cols", "t") == 0   # empty


@pytest.mark.integration
def test_build_empty_from_columns_rebuilds_on_rerun():
    """A second call drops and recreates the table rather than merging columns."""
    a = PostgresAdapter(_conn())
    a.ensure_schema("it_cols")
    a.build_empty_from_columns("it_cols", "t2", [{"name": "id", "type": "INTEGER"}], {})
    a.build_empty_from_columns("it_cols", "t2", [{"name": "renamed", "type": "BIGINT"}], {})
    a.close()
    assert [c[0] for c in _columns("it_cols", "t2")] == ["renamed"]  # drop-then-create


@pytest.mark.integration
def test_check_binds_passes_on_valid_read_against_empty_upstream():
    """check_binds does not raise for a query that binds against known columns."""
    a = PostgresAdapter(_conn())
    a.ensure_schema("it_binds")
    a.build_empty_from_columns("it_binds", "up", [
        {"name": "a", "type": "INTEGER"}, {"name": "b", "type": "TEXT"},
    ], {})
    a.check_binds("select a, b from it_binds.up")  # must not raise
    a.close()


@pytest.mark.integration
def test_check_binds_passes_on_read_ending_in_line_comment():
    """A read ending in a trailing `--` comment must not swallow the wrap's close-paren."""
    a = PostgresAdapter(_conn())
    a.ensure_schema("it_binds")
    a.build_empty_from_columns("it_binds", "up5", [
        {"name": "a", "type": "INTEGER"}, {"name": "b", "type": "TEXT"},
    ], {})
    a.check_binds("select a, b from it_binds.up5 -- trailing comment")  # must not raise
    a.close()


@pytest.mark.integration
def test_check_binds_raises_on_missing_column():
    """check_binds raises when a selected column does not exist upstream."""
    a = PostgresAdapter(_conn())
    a.ensure_schema("it_binds")
    a.build_empty_from_columns("it_binds", "up2", [{"name": "a", "type": "INTEGER"}], {})
    with pytest.raises(Exception):
        a.check_binds("select a, dropped_col from it_binds.up2")
    a.close()


@pytest.mark.integration
def test_check_binds_raises_on_missing_table():
    """check_binds raises when the referenced table does not exist."""
    a = PostgresAdapter(_conn())
    with pytest.raises(Exception):
        a.check_binds("select 1 from it_binds.does_not_exist")
    a.close()


@pytest.mark.integration
def test_check_binds_rejects_stacked_statement_and_executes_nothing():
    """A stacked statement is rejected at parse time; the appended DML never runs."""
    a = PostgresAdapter(_conn())
    a.ensure_schema("it_binds")
    a.build_empty_from_columns("it_binds", "up3", [{"name": "a", "type": "INTEGER"}], {})
    conn = _conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("INSERT INTO it_binds.up3 VALUES (1), (2), (3)")
    conn.close()
    with pytest.raises(Exception):
        a.check_binds("select a from it_binds.up3; delete from it_binds.up3")
    a.close()
    assert _count("it_binds", "up3") == 3  # the appended DELETE never executed


@pytest.mark.integration
def test_check_binds_rejects_bare_dml():
    """Bare DML fails the bind check instead of silently EXPLAIN-passing."""
    a = PostgresAdapter(_conn())
    a.ensure_schema("it_binds")
    a.build_empty_from_columns("it_binds", "up4", [{"name": "a", "type": "INTEGER"}], {})
    conn = _conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("INSERT INTO it_binds.up4 VALUES (1), (2)")
    conn.close()
    with pytest.raises(Exception):
        a.check_binds("delete from it_binds.up4")
    a.close()
    assert _count("it_binds", "up4") == 2  # the DELETE never executed


def _seeded_bind_table(table: str, rows: int = 3) -> None:
    """Build it_binds.<table> and seed it with *rows* rows."""
    a = PostgresAdapter(_conn())
    a.ensure_schema("it_binds")
    a.build_empty_from_columns("it_binds", table, [{"name": "a", "type": "INTEGER"}], {})
    a.close()
    conn = _conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO it_binds.{table} "
            f"VALUES {', '.join(f'({i})' for i in range(1, rows + 1))}"
        )
    conn.close()


def _table_exists(schema: str, table: str) -> bool:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=%s AND table_name=%s",
            (schema, table),
        )
        found = cur.fetchone() is not None
    conn.close()
    return found


@pytest.mark.integration
def test_check_binds_rejects_paren_balanced_escape_and_executes_nothing():
    """A read that balances the wrap's own parens must not smuggle a DELETE past it.

    The subquery wrap alone does not stop this: the read closes the wrap's
    opening paren and reopens it after the injected statement, leaving the
    wrapped text balanced, and psycopg2's simple-query protocol then executes
    every ``;``-separated statement in the batch. The row count is the
    assertion that matters — a rejection raised *after* the DELETE ran would
    still satisfy pytest.raises.
    """
    _seeded_bind_table("up6")
    a = PostgresAdapter(_conn())
    with pytest.raises(Exception):
        a.check_binds("select 1) AS x; DELETE FROM it_binds.up6; SELECT * FROM (SELECT 1")
    a.close()
    assert _count("it_binds", "up6") == 3  # the injected DELETE never executed


@pytest.mark.integration
def test_check_binds_rejects_paren_balanced_escape_carrying_ddl():
    """The same escape carrying a DROP TABLE must leave the table standing."""
    _seeded_bind_table("up7")
    a = PostgresAdapter(_conn())
    with pytest.raises(Exception):
        a.check_binds("select 1) AS x; DROP TABLE it_binds.up7; SELECT * FROM (SELECT 1")
    a.close()
    assert _table_exists("it_binds", "up7")  # the injected DROP never executed
    assert _count("it_binds", "up7") == 3


@pytest.mark.integration
def test_check_binds_leaves_the_connection_usable_after_a_failed_read():
    """A failed bind check must not poison the connection for the reads after it.

    check_binds runs inside an explicit read-only transaction; if the ROLLBACK
    were skipped on the error path the session would sit in a failed
    transaction and every later read in the same Job would fail too.
    """
    a = PostgresAdapter(_conn())
    a.ensure_schema("it_binds")
    a.build_empty_from_columns("it_binds", "up8", [{"name": "a", "type": "INTEGER"}], {})
    with pytest.raises(Exception):
        a.check_binds("select a, missing_col from it_binds.up8")
    a.check_binds("select a from it_binds.up8")  # same adapter, must not raise
    a.close()


@pytest.mark.integration
def test_config_indexes_are_applied_to_the_built_table():
    """A valid config yields real indexes on the created table, unique and type honored."""
    a = PostgresAdapter(_conn())
    a.ensure_schema("it_cfg")
    a.build_empty_from_columns(
        "it_cfg", "laid_out",
        [
            {"name": "id", "type": "INTEGER", "nullable": False},
            {"name": "created_at", "type": "TIMESTAMP"},
        ],
        {"indexes": [
            {"columns": ["id"], "unique": True},
            {"columns": ["created_at"], "type": "brin"},
        ]},
    )
    a.close()
    defs = " | ".join(d for _, d in _indexes("it_cfg", "laid_out"))
    assert "CREATE UNIQUE INDEX" in defs
    assert "USING btree (id)" in defs
    assert "USING brin (created_at)" in defs


@pytest.mark.integration
def test_config_indexes_survive_a_rebuild():
    """Drop-then-create must recreate the indexes under the same deterministic names."""
    a = PostgresAdapter(_conn())
    a.ensure_schema("it_cfg")
    config = {"indexes": [{"columns": ["id"], "unique": True}]}
    columns = [{"name": "id", "type": "INTEGER", "nullable": False}]
    a.build_empty_from_columns("it_cfg", "rebuilt", columns, config)
    first = [name for name, _ in _indexes("it_cfg", "rebuilt")]
    a.build_empty_from_columns("it_cfg", "rebuilt", columns, config)
    a.close()
    assert [name for name, _ in _indexes("it_cfg", "rebuilt")] == first
    assert first  # and there really was an index to begin with


@pytest.mark.integration
def test_unknown_config_key_rejects_and_leaves_no_table():
    """Fail closed: an unknown key rejects before the DROP, so nothing is half-built."""
    a = PostgresAdapter(_conn())
    a.ensure_schema("it_cfg")
    with pytest.raises(ValueError, match="sortkey"):
        a.build_empty_from_columns(
            "it_cfg", "never_built", [{"name": "id", "type": "INTEGER"}],
            {"sortkey": ["id"]},
        )
    a.close()
    assert not _table_exists("it_cfg", "never_built")


@pytest.mark.integration
def test_index_on_nonexistent_column_rejects_and_leaves_no_table():
    """An index naming a column the node does not declare fails the gate."""
    a = PostgresAdapter(_conn())
    a.ensure_schema("it_cfg")
    with pytest.raises(ValueError, match="nope"):
        a.build_empty_from_columns(
            "it_cfg", "also_never_built", [{"name": "id", "type": "INTEGER"}],
            {"indexes": [{"columns": ["nope"]}]},
        )
    a.close()
    assert not _table_exists("it_cfg", "also_never_built")


@pytest.mark.integration
def test_empty_config_creates_no_indexes():
    """Back-compat pin: no config means the bare 0.4.0 CREATE TABLE and nothing else."""
    a = PostgresAdapter(_conn())
    a.ensure_schema("it_cfg")
    a.build_empty_from_columns(
        "it_cfg", "bare", [{"name": "id", "type": "INTEGER", "nullable": False}], {}
    )
    a.close()
    assert _indexes("it_cfg", "bare") == []
    assert _columns("it_cfg", "bare") == [("id", "integer")]


@pytest.mark.integration
def test_an_index_the_engine_rejects_rolls_the_whole_rebuild_back():
    """A rebuild is atomic: an index postgres refuses leaves the prior table intact.

    Not every rejection can be caught in python — `gin` on an integer column
    fails for want of a default operator class, which depends on the column's
    type and on what extensions the database has installed. Without a
    transaction the DROP and CREATE would already have committed by then,
    leaving a table rebuilt and missing the layout its author declared. The
    assertion that matters is the column list: it must still be the FIRST
    build's, proving the second build's DROP rolled back rather than the
    engine error merely propagating.
    """
    a = PostgresAdapter(_conn())
    a.ensure_schema("it_cfg")
    a.build_empty_from_columns(
        "it_cfg", "atomic", [{"name": "original", "type": "INTEGER"}], {}
    )
    with pytest.raises(Exception):
        a.build_empty_from_columns(
            "it_cfg", "atomic", [{"name": "replacement", "type": "INTEGER"}],
            {"indexes": [{"columns": ["replacement"], "type": "gin"}]},
        )
    assert [c[0] for c in _columns("it_cfg", "atomic")] == ["original"]
    assert _indexes("it_cfg", "atomic") == []
    # The connection must survive the failed transaction, or every later node
    # op in the same Job would fail on a poisoned session.
    a.build_empty_from_columns(
        "it_cfg", "atomic", [{"name": "third", "type": "INTEGER"}],
        {"indexes": [{"columns": ["third"], "unique": True}]},
    )
    a.close()
    assert [c[0] for c in _columns("it_cfg", "atomic")] == ["third"]
    assert len(_indexes("it_cfg", "atomic")) == 1
