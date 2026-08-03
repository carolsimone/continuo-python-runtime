"""Unit tests for the trino runtime adapter — pure-logic, mock-free.

DDL/swap behavior (ensure_table's CREATE TABLE, load's staged rename-swap, the
trino connection built by from_env) is verified against a live Trino + Iceberg
stack in test_integration_runtime_trino.py, not with mocked cursors/connections
here (see the repo CLAUDE.md: DDL behavior belongs behind live-engine tests).
What's exercised here is pure Python logic that never touches a connection: the
SQL type-grammar injection guard, the type-name mapping to Trino spellings, the
identifier quoting helper, and the Arrow-table construction helper.
"""
from importlib.metadata import entry_points

import pyarrow as pa
import pytest

import continuo_python_runtime_trino.adapter as adapter_module

from continuo_python_runtime_trino.adapter import (
    TrinoRuntimeAdapter,
    _arrow_table_from_rows,
    _quote,
    _trino_type,
    _validate_column_type,
)


def test_required_env_names_host_and_catalog():
    """Test that required_env lists exactly the two mandatory TRINO_* vars."""
    assert TrinoRuntimeAdapter.required_env() == ["TRINO_HOST", "TRINO_CATALOG"]


def test_entry_point_registered_and_loads_adapter():
    """Test that the trino runtime entry point loads TrinoRuntimeAdapter."""
    eps = [ep for ep in entry_points(group="continuo_runtime.adapters")
           if ep.name == "trino"]
    assert len(eps) == 1
    assert eps[0].load() is TrinoRuntimeAdapter


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
    fullwidth '１０' == '10'), which would let a lookalike length sneak past the
    injection guard before being interpolated into DDL.
    """
    with pytest.raises(ValueError):
        _validate_column_type("VARCHAR(１０)")  # fullwidth "10"


def test_validate_column_type_rejects_trailing_newline():
    r"""Test that a trailing newline after the type text is rejected (\Z, not $)."""
    with pytest.raises(ValueError):
        _validate_column_type("TEXT\n")


@pytest.mark.parametrize(
    ("type_str", "expected"),
    [
        ("TEXT", "VARCHAR"),
        ("text", "VARCHAR"),
        ("DOUBLE PRECISION", "DOUBLE"),
        ("double precision", "DOUBLE"),
        ("NUMERIC(10,2)", "DECIMAL(10,2)"),
        ("numeric(10, 2)", "DECIMAL(10, 2)"),
        ("BIGINT", "BIGINT"),
        ("INT", "INT"),
        ("INTEGER", "INTEGER"),
        ("BOOLEAN", "BOOLEAN"),
        ("TIMESTAMP", "TIMESTAMP"),
        ("DATE", "DATE"),
        ("VARCHAR(255)", "VARCHAR(255)"),
        ("CHAR(1)", "CHAR(1)"),
        ("DECIMAL(5,0)", "DECIMAL(5,0)"),
    ],
)
def test_trino_type_maps_grammar_to_trino_spelling(type_str, expected):
    """Test that TEXT/DOUBLE PRECISION/NUMERIC map to Trino's own type names.

    Every other grammar token is valid Trino DDL unchanged (verified live
    against Trino 483 + the Iceberg connector; see the module docstring).
    """
    assert _trino_type(type_str).upper().replace(" ", "") == expected.upper().replace(" ", "")


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("simple", '"simple"'),
        ("sales-prod", '"sales-prod"'),
        ("order id", '"order id"'),
        ('has"quote', '"has""quote"'),
        ("rate%", '"rate%"'),
        ("has.dot", '"has.dot"'),
        ("has;semicolon", '"has;semicolon"'),
    ],
)
def test_quote_preserves_legal_delimited_identifiers(identifier, expected):
    """Test that valid delimited names are escaped without narrowing their grammar."""
    assert _quote(identifier) == expected


def test_quote_rejects_empty_identifier():
    """Test that the contract's non-empty identifier requirement is enforced."""
    with pytest.raises(ValueError, match="empty"):
        _quote("")


@pytest.mark.parametrize(
    ("location", "name", "expected"),
    [
        (
            "s3://warehouse/schema/target-a1",
            "__continuo_stage_b2",
            "s3://warehouse/schema/__continuo_stage_b2",
        ),
        (
            "s3://warehouse/target-a1/",
            "__continuo_stage_b2",
            "s3://warehouse/__continuo_stage_b2",
        ),
        (
            "/warehouse/schema/target-a1",
            "__continuo_stage_b2",
            "/warehouse/schema/__continuo_stage_b2",
        ),
    ],
)
def test_sibling_location_replaces_only_the_final_path_component(
    location, name, expected
):
    """Test that a private location stays beside the target across URI shapes."""
    assert adapter_module._sibling_location(location, name) == expected


def test_sql_string_escapes_embedded_quotes():
    """Test that metadata-derived locations remain one SQL string literal."""
    assert adapter_module._sql_string("s3://warehouse/o'hare") == (
        "'s3://warehouse/o''hare'"
    )


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


def test_arrow_table_from_rows_accepts_list_rows():
    """Test that list-shaped rows (as the trino client returns) work, not just tuples."""
    table = _arrow_table_from_rows(["id", "name"], [[1, "a"], [2, "b"]])
    assert table.num_rows == 2
    assert table.column("id").to_pylist() == [1, 2]


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
