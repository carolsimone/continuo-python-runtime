"""Unit tests for the postgres runtime adapter — pure-logic, mock-free.

DDL/transactional behavior (ensure_table's CREATE TABLE, load's TRUNCATE +
batched insert + rollback-on-error, the psycopg2 connection built by from_env)
is verified against a live postgres engine in
test_integration_runtime_postgres.py, not with mocked cursors/connections here
(see the repo CLAUDE.md: DDL behavior belongs behind live-engine tests). What's
exercised here is pure Python logic that never touches a connection: the SQL
type-grammar injection guard and the Arrow-table construction helper.
"""
from importlib.metadata import entry_points

import pyarrow as pa
import pytest

from continuo_python_runtime_postgres.adapter import (
    PostgresRuntimeAdapter,
    _arrow_table_from_rows,
    _validate_column_type,
)


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
    _validate_column_type(type_str)  # must not raise


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
        _validate_column_type(type_str)


def test_validate_column_type_rejects_non_ascii_digits():
    r"""Test that non-ASCII (e.g. fullwidth) digits don't satisfy \d under the grammar.

    Without re.ASCII, Python's \d matches Unicode decimal digits too (e.g. the
    fullwidth '１０' == '10'), which would let a lookalike length sneak past
    the injection guard before being interpolated into DDL.
    """
    with pytest.raises(ValueError):
        _validate_column_type("VARCHAR(１０)")  # fullwidth "10"


def test_validate_column_type_rejects_trailing_newline():
    r"""Test that a trailing newline after the type text is rejected (\Z, not $)."""
    with pytest.raises(ValueError):
        _validate_column_type("TEXT\n")


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
