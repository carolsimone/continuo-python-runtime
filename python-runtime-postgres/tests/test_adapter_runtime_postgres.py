"""Unit tests for the postgres runtime adapter — pure-logic, mock-free.

Transactional/live DDL behavior (load's TRUNCATE + batched insert +
rollback-on-error, the psycopg2 connection built by from_env, and DDL validity
against a real server) is verified against a live postgres engine in
test_integration_runtime_postgres.py, not with mocked cursors/connections here.

``ensure_table``'s ``config`` handling — the fail-closed vocabulary
enforcement this module adds — is a different kind of thing to test: what
matters is which statements get built and in what order, not how postgres
executes them. ``psycopg2.sql.Composable.as_string()`` requires a real,
connected psycopg2 connection/cursor to render (it calls the C-level
``PQescapeIdentifier``), so a live engine is not avoidable if the test wants a
rendered SQL string. Instead, ``_FakeConnection``/``_FakeCursor`` below record
the raw ``Composable`` objects passed to ``execute()``, and tests assert
structural equality against a ``Composable`` built the same way — psycopg2's
``sql`` objects support ``==`` — which needs no connection at all. This is the
one fake used for every ``ensure_table`` test in this file, positive and
negative alike.
"""
from importlib.metadata import entry_points
from typing import Any

import pyarrow as pa
import pytest
from continuo_validation_contract.types import validate_column_type  # type: ignore[import-untyped]

from continuo_python_runtime_postgres.adapter import (
    PostgresRuntimeAdapter,
    _arrow_table_from_rows,
    _index_name,
)
from psycopg2 import sql as pg_sql


class _FakeCursor:
    """Minimal cursor double: records every statement passed to execute() on itself.

    ``ensure_table`` opens ``_ensure_schema``'s advisory-lock/CREATE SCHEMA/unlock
    sequence in its own ``with self._conn.cursor()`` block, then a second,
    separate cursor block for the table + index DDL under test — so each call
    to ``cursor()`` gets its own statement list, keeping the two sequences
    apart without the test needing to know how many statements schema-creation
    happens to take.
    """

    def __init__(self) -> None:
        self.statements: list[Any] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False

    def execute(self, statement: Any, params: Any = None) -> None:
        self.statements.append(statement)


class _FakeConnection:
    """Minimal connection double: hands out ``_FakeCursor``\\ s, tracks commit/rollback.

    ``autocommit`` is a plain settable attribute — ``PostgresRuntimeAdapter.__init__``
    assigns it directly, no property needed for a test double.
    """

    def __init__(self) -> None:
        self.cursors: list[_FakeCursor] = []
        self.autocommit = False
        self.committed = 0
        self.rolled_back = 0

    def cursor(self) -> _FakeCursor:
        cur = _FakeCursor()
        self.cursors.append(cur)
        return cur

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1


def test_required_env_names_connection_vars():
    """Test that required_env lists the three mandatory POSTGRES_* vars."""
    assert PostgresRuntimeAdapter.required_env() == [
        "POSTGRES_HOST", "POSTGRES_DB", "POSTGRES_USER",
    ]


def test_entry_point_registered_and_loads_adapter():
    """Test that the postgres runtime entry point loads PostgresRuntimeAdapter."""
    eps = [ep for ep in entry_points(group="continuo_runtime.adapters")
           if ep.name == "postgres"]
    assert len(eps) == 1
    assert eps[0].load() is PostgresRuntimeAdapter


@pytest.mark.parametrize(
    "type_str",
    [
        "BIGINT", "INT", "INTEGER", "DOUBLE PRECISION", "TEXT", "TIMESTAMP",
        "DATE", "BOOLEAN", "NUMERIC(10,2)", "NUMERIC(10, 2)", "DECIMAL(5,0)",
        "VARCHAR(255)", "CHAR(1)", "bigint", "varchar(1)",
    ],
)
def test_validate_column_type_accepts_grammar(type_str):
    """Test that every SQL type in the contract's grammar is accepted."""
    validate_column_type(type_str)  # must not raise


@pytest.mark.parametrize(
    "type_str",
    [
        "TEXT); DROP TABLE x; --",
        "VARCHAR",
        "NUMERIC",
        "FLOAT",
        "",
        "BIGINT;",
        "VARCHAR(-1)",
        "TEXT, TEXT",
    ],
)
def test_validate_column_type_rejects_unknown_or_malicious(type_str):
    """Test that unknown or injection-shaped type strings raise ValueError."""
    with pytest.raises(ValueError):
        validate_column_type(type_str)


def test_validate_column_type_rejects_non_ascii_digits():
    r"""Test that non-ASCII (e.g. fullwidth) digits don't satisfy \d under the grammar.

    Without re.ASCII, Python's \d matches Unicode decimal digits too (e.g. the
    fullwidth '１０' == '10'), which would let a lookalike length sneak past
    the injection guard before being interpolated into DDL.
    """
    with pytest.raises(ValueError):
        validate_column_type("VARCHAR(１０)")  # fullwidth "10"


def test_validate_column_type_rejects_trailing_newline():
    r"""Test that a trailing newline after the type text is rejected (\Z, not $)."""
    with pytest.raises(ValueError):
        validate_column_type("TEXT\n")


def test_arrow_table_from_rows_duplicate_column_names_raise():
    """Test that duplicate SELECT column names raise instead of silently dropping data."""
    with pytest.raises(ValueError, match="id"):
        _arrow_table_from_rows(["id", "id"], [(1, 2)])


def test_arrow_table_from_rows_builds_column_wise():
    """Test column-wise Arrow construction from cursor description + fetched rows."""
    table = _arrow_table_from_rows(["id", "name"], [(1, "a"), (2, None)])
    assert table.num_rows == 2
    assert table.column_names == ["id", "name"]
    assert table.column("id").to_pylist() == [1, 2]
    assert table.column("name").to_pylist() == ["a", None]


def test_arrow_table_from_rows_empty_result_is_null_typed():
    """Test that an empty result yields a 0-row table with null-typed columns."""
    table = _arrow_table_from_rows(["id", "name"], [])
    assert table.num_rows == 0
    assert table.column_names == ["id", "name"]
    assert table.column("id").type == pa.null()
    assert table.column("name").type == pa.null()


def test_arrow_table_from_rows_no_columns_no_rows():
    """Test that zero columns and zero rows yields an empty table, not an error."""
    table = _arrow_table_from_rows([], [])
    assert table.num_rows == 0
    assert table.column_names == []


# --- ensure_table's `config` (physical-layout indexes), failing closed ---


def test_index_name_derives_from_table_and_columns():
    """Test the default index name shape when no explicit `name` is given."""
    assert _index_name("orders", ["a", "b"]) == "ix_orders_a_b"


def test_index_name_truncates_to_postgres_identifier_limit():
    """Test that a derived name over 63 bytes is truncated to exactly 63 bytes.

    Truncating here keeps the emitted DDL's index name equal to the name
    postgres itself would store (NAMEDATALEN silently truncates longer
    identifiers), so the truncation must happen before the name reaches DDL.
    """
    columns = ["a_very_long_column_name"] * 4
    full = f"ix_a_table_with_quite_a_long_name_{'_'.join(columns)}"
    name = _index_name("a_table_with_quite_a_long_name", columns)
    assert len(name.encode("utf-8")) == 63
    assert full.startswith(name)


_ONE_COL = [{"name": "id", "type": "INT", "nullable": True}]


def test_ensure_table_with_none_config_emits_no_index_ddl():
    """Test that config=None creates the table and nothing else.

    ``_ensure_schema`` opens its own earlier cursor for the advisory-lock/
    CREATE SCHEMA/unlock sequence; ``conn.cursors[-1]`` is the table+index
    cursor under test here.
    """
    conn = _FakeConnection()
    adapter = PostgresRuntimeAdapter(conn)
    adapter.ensure_table("s", "t", _ONE_COL, config=None)
    assert len(conn.cursors[-1].statements) == 1  # CREATE TABLE only
    assert conn.rolled_back == 0


def test_ensure_table_with_empty_config_emits_no_index_ddl():
    """Test that config={} creates the table and nothing else."""
    conn = _FakeConnection()
    adapter = PostgresRuntimeAdapter(conn)
    adapter.ensure_table("s", "t", _ONE_COL, config={})
    assert len(conn.cursors[-1].statements) == 1  # CREATE TABLE only
    assert conn.rolled_back == 0


def test_ensure_table_single_column_index_emits_expected_create_index():
    """Test that a valid single-column index emits the expected CREATE INDEX statement."""
    conn = _FakeConnection()
    adapter = PostgresRuntimeAdapter(conn)
    adapter.ensure_table("s", "t", _ONE_COL, config={"indexes": [{"columns": ["id"]}]})
    statements = conn.cursors[-1].statements
    assert len(statements) == 2
    expected = pg_sql.SQL("CREATE {}INDEX IF NOT EXISTS {} ON {}.{} ({})").format(
        pg_sql.SQL(""),
        pg_sql.Identifier("ix_t_id"),
        pg_sql.Identifier("s"),
        pg_sql.Identifier("t"),
        pg_sql.SQL(", ").join([pg_sql.Identifier("id")]),
    )
    assert statements[1] == expected
    assert conn.rolled_back == 0


def test_ensure_table_unique_index_emits_create_unique_index():
    """Test that `unique: true` emits CREATE UNIQUE INDEX."""
    conn = _FakeConnection()
    adapter = PostgresRuntimeAdapter(conn)
    adapter.ensure_table(
        "s", "t", _ONE_COL, config={"indexes": [{"columns": ["id"], "unique": True}]}
    )
    expected = pg_sql.SQL("CREATE {}INDEX IF NOT EXISTS {} ON {}.{} ({})").format(
        pg_sql.SQL("UNIQUE "),
        pg_sql.Identifier("ix_t_id"),
        pg_sql.Identifier("s"),
        pg_sql.Identifier("t"),
        pg_sql.SQL(", ").join([pg_sql.Identifier("id")]),
    )
    assert conn.cursors[-1].statements[1] == expected


def test_ensure_table_custom_index_name_is_used():
    """Test that an explicit `name` overrides the derived name."""
    conn = _FakeConnection()
    adapter = PostgresRuntimeAdapter(conn)
    adapter.ensure_table(
        "s", "t", _ONE_COL, config={"indexes": [{"columns": ["id"], "name": "ix_custom"}]}
    )
    expected = pg_sql.SQL("CREATE {}INDEX IF NOT EXISTS {} ON {}.{} ({})").format(
        pg_sql.SQL(""),
        pg_sql.Identifier("ix_custom"),
        pg_sql.Identifier("s"),
        pg_sql.Identifier("t"),
        pg_sql.SQL(", ").join([pg_sql.Identifier("id")]),
    )
    assert conn.cursors[-1].statements[1] == expected


def test_ensure_table_multiple_indexes_all_run_in_one_cursor_block():
    """Test that multiple indexes each emit a statement, in the same cursor block as the table."""
    conn = _FakeConnection()
    adapter = PostgresRuntimeAdapter(conn)
    cols = [
        {"name": "id", "type": "INT", "nullable": True},
        {"name": "email", "type": "TEXT", "nullable": True},
    ]
    adapter.ensure_table(
        "s", "t", cols,
        config={"indexes": [{"columns": ["id"]}, {"columns": ["email"], "unique": True}]},
    )
    assert len(conn.cursors[-1].statements) == 3  # CREATE TABLE + 2 indexes
    assert conn.rolled_back == 0


_BAD_CONFIGS = [
        pytest.param({"sortkey": ["id"]}, "sortkey", id="unknown_top_level_key"),
        pytest.param(
            {"indexes": {"columns": ["id"]}}, "must be a list", id="indexes_not_a_list"
        ),
        pytest.param({"indexes": ["id"]}, "mapping", id="index_entry_not_a_mapping"),
        pytest.param(
            {"indexes": [{"columns": ["id"], "method": "brin"}]},
            "method",
            id="unrecognized_index_key",
        ),
        pytest.param({"indexes": [{}]}, "columns", id="columns_missing"),
        pytest.param({"indexes": [{"columns": "id"}]}, "columns", id="columns_not_a_list"),
        pytest.param({"indexes": [{"columns": []}]}, "columns", id="columns_empty"),
        pytest.param(
            {"indexes": [{"columns": ["id", 3]}]}, "columns", id="columns_contains_non_string"
        ),
        pytest.param(
            {"indexes": [{"columns": ["nope"]}]}, "nope", id="index_on_undeclared_column"
        ),
        pytest.param(
            {"indexes": [{"columns": ["id"], "unique": "yes"}]}, "unique",
            id="unique_not_a_bool",
        ),
        pytest.param(
            {"indexes": [{"columns": ["id"], "name": ""}]}, "name", id="name_empty_string"
        ),
        pytest.param(
            {"indexes": [{"columns": ["id"], "name": 123}]}, "name", id="name_not_a_string"
        ),
]


@pytest.mark.parametrize(("config", "match"), _BAD_CONFIGS)
def test_ensure_table_rejects_bad_config_and_emits_no_ddl(config, match):
    """Test that every rejection rule raises ValueError naming the key and runs no DDL.

    Config validation is the very first thing ``ensure_table`` does, before
    schema creation or table creation — so a rejection here means no cursor
    was ever obtained at all: ``conn.cursors`` stays empty.
    """
    conn = _FakeConnection()
    adapter = PostgresRuntimeAdapter(conn)
    with pytest.raises(ValueError, match=match):
        adapter.ensure_table("s", "t", _ONE_COL, config=config)
    assert conn.cursors == []
    assert conn.committed == 0
    assert conn.rolled_back == 0


def test_ensure_table_bad_column_type_still_rejects_and_emits_no_ddl():
    """Test that config validation does not skip the pre-existing column-type guard.

    Column-type validation runs right after config validation but still before
    any cursor is obtained, so this also leaves ``conn.cursors`` empty.
    """
    conn = _FakeConnection()
    adapter = PostgresRuntimeAdapter(conn)
    with pytest.raises(ValueError):
        adapter.ensure_table(
            "s", "t", [{"name": "id", "type": "NOT_A_TYPE", "nullable": True}], config=None
        )
    assert conn.cursors == []


# --- validate_config: the harness's early tripwire ---
#
# ensure_table stays the enforcement point; validate_config only lets the
# harness reject a bad config in the first second of a run instead of after
# the script has already computed its whole result. It reuses
# _validated_indexes, so it cannot drift from what ensure_table enforces.


@pytest.mark.parametrize(("config", "match"), _BAD_CONFIGS)
def test_validate_config_rejects_everything_ensure_table_rejects(config, match):
    with pytest.raises(ValueError, match=match):
        PostgresRuntimeAdapter.validate_config(config, ["id"])


@pytest.mark.parametrize("config", [None, {}, {"indexes": []}, {"indexes": [
    {"columns": ["id"], "unique": True, "name": "ix_custom"},
]}])
def test_validate_config_accepts_what_ensure_table_accepts(config):
    assert PostgresRuntimeAdapter.validate_config(config, ["id"]) is None


def test_validate_config_is_a_classmethod_needing_no_connection():
    """The harness calls it before any adapter I/O; it must not need a connection."""
    with pytest.raises(ValueError, match="nope"):
        PostgresRuntimeAdapter.validate_config({"indexes": [{"columns": ["nope"]}]}, ["id"])
