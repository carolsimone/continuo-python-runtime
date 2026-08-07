"""Unit tests for the trino runtime adapter — pure-logic, mock-free.

Transactional/live DDL behavior (load's staged rename-swap, the trino
connection built by from_env, and DDL validity against a real server) is
verified against a live Trino + Iceberg stack in
test_integration_runtime_trino.py, not with mocked cursors/connections here.

``ensure_table``'s ``config`` handling — the fail-closed vocabulary
enforcement this module adds — renders to a plain Trino SQL string (unlike the
postgres adapter's ``psycopg2.sql`` composables), so most of it is exercised
as pure Python logic via ``_table_properties`` with no connection at all. A
small ``_FakeConnection``/``_FakeCursor`` (the one fake used for every
``ensure_table`` test in this file) additionally proves the wiring: that the
rendered properties clause actually lands in the emitted ``CREATE TABLE``
statement, and that a rejected config reaches the connection not at all.
"""
from importlib.metadata import entry_points
from typing import Any

import pyarrow as pa
import pytest
from continuo_validation_contract.types import validate_column_type  # type: ignore[import-untyped]

import continuo_python_runtime_trino.adapter as adapter_module

from continuo_python_runtime_trino.adapter import (
    TrinoRuntimeAdapter,
    _arrow_table_from_rows,
    _quote,
    _table_properties,
    _trino_type,
)


class _FakeCursor:
    """Minimal cursor double: records the statement passed to execute()."""

    def __init__(self, conn: "_FakeConnection") -> None:
        self._conn = conn

    def execute(self, statement: str, params: Any = None) -> None:
        self._conn.statements.append(statement)

    def fetchall(self) -> list[Any]:
        return []

    def close(self) -> None:
        pass


class _FakeConnection:
    """Minimal connection double: every ``_execute`` call opens a fresh cursor.

    Unlike the postgres adapter, trino's ``ensure_table`` never opens more
    than one statement per cursor, and ``_ensure_schema`` + the final
    ``CREATE TABLE`` both flow through ``TrinoRuntimeAdapter._execute``, so a
    single shared ``statements`` list is enough: a successful call appends
    exactly ``[CREATE SCHEMA ..., CREATE TABLE ...]``, in that order.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


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
    fullwidth '１０' == '10'), which would let a lookalike length sneak past the
    injection guard before being interpolated into DDL.
    """
    with pytest.raises(ValueError):
        validate_column_type("VARCHAR(１０)")  # fullwidth "10"


def test_validate_column_type_rejects_trailing_newline():
    r"""Test that a trailing newline after the type text is rejected (\Z, not $)."""
    with pytest.raises(ValueError):
        validate_column_type("TEXT\n")


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


# --- ensure_table's `config` (Iceberg physical-layout properties), failing closed ---


def test_table_properties_no_config_renders_no_with_clause():
    """Test that config=None renders the bare CREATE TABLE, no WITH clause."""
    assert _table_properties(None) == ""


def test_table_properties_empty_config_renders_no_with_clause():
    """Test that config={} renders the bare CREATE TABLE, no WITH clause."""
    assert _table_properties({}) == ""


def test_table_properties_partitioning_alone():
    """Test that `partitioning` alone renders as its own WITH clause."""
    assert _table_properties({"partitioning": ["day(event_ts)", "region"]}) == (
        " WITH (partitioning = ARRAY['day(event_ts)', 'region'])"
    )


def test_table_properties_sorted_by_alone():
    """Test that `sorted_by` alone renders as its own WITH clause."""
    assert _table_properties({"sorted_by": ["id"]}) == " WITH (sorted_by = ARRAY['id'])"


def test_table_properties_all_three_in_fixed_order_regardless_of_input_order():
    """Test that partitioning, sorted_by, format always render in that fixed order.

    DDL must be deterministic: the config mapping's own key order must not
    change the emitted statement.
    """
    forward = _table_properties(
        {"partitioning": ["day(event_ts)"], "sorted_by": ["id"], "format": "PARQUET"}
    )
    reversed_ = _table_properties(
        {"format": "PARQUET", "sorted_by": ["id"], "partitioning": ["day(event_ts)"]}
    )
    assert forward == reversed_
    assert forward == (
        " WITH (partitioning = ARRAY['day(event_ts)'], sorted_by = ARRAY['id'], "
        "format = 'PARQUET')"
    )


def test_table_properties_escapes_embedded_quote():
    """Test that a partition transform value containing `'` is escaped, not broken."""
    assert _table_properties({"partitioning": ["o'hare"]}) == (
        " WITH (partitioning = ARRAY['o''hare'])"
    )


def test_table_properties_format_is_case_normalized_to_upper():
    """Test that a lowercase format value is accepted and rendered canonically."""
    assert _table_properties({"format": "parquet"}) == " WITH (format = 'PARQUET')"


def test_table_properties_rejects_hive_partitioned_by_spelling():
    """Test that `partitioned_by` (the Hive connector's spelling) is rejected.

    This adapter targets the Iceberg connector, whose own property is
    `partitioning` — this is a deliberate decision of record, not an oversight
    to "correct" toward the Hive spelling.
    """
    with pytest.raises(ValueError, match="partitioned_by"):
        _table_properties({"partitioned_by": ["event_ts"]})


@pytest.mark.parametrize(
    ("config", "match"),
    [
        pytest.param({"sortkey": ["id"]}, "sortkey", id="unknown_key"),
        pytest.param({"partitioning": "id"}, "partitioning", id="partitioning_not_a_list"),
        pytest.param({"partitioning": []}, "partitioning", id="partitioning_empty"),
        pytest.param(
            {"partitioning": ["id", 3]}, "partitioning", id="partitioning_non_string_element"
        ),
        pytest.param(
            {"partitioning": ["id", ""]}, "partitioning", id="partitioning_empty_string_element"
        ),
        pytest.param({"sorted_by": "id"}, "sorted_by", id="sorted_by_not_a_list"),
        pytest.param({"sorted_by": []}, "sorted_by", id="sorted_by_empty"),
        pytest.param(
            {"sorted_by": ["id", 3]}, "sorted_by", id="sorted_by_non_string_element"
        ),
        pytest.param(
            {"sorted_by": ["id", ""]}, "sorted_by", id="sorted_by_empty_string_element"
        ),
        pytest.param({"format": 3}, "format", id="format_not_a_string"),
        pytest.param({"format": None}, "format", id="format_none"),
        pytest.param({"format": "AVRO2"}, "format", id="format_not_in_allowlist"),
    ],
)
def test_table_properties_rejects_bad_config(config, match):
    """Test that every rejection rule raises ValueError naming the offending key."""
    with pytest.raises(ValueError, match=match):
        _table_properties(config)


_ONE_COL = [{"name": "id", "type": "INT", "nullable": True}]


def _adapter(conn: "_FakeConnection") -> TrinoRuntimeAdapter:
    return TrinoRuntimeAdapter(conn, "iceberg")


def test_ensure_table_no_recognized_config_key_statement_has_no_with():
    """Test that an ensure_table call with config=None emits a bare CREATE TABLE."""
    conn = _FakeConnection()
    _adapter(conn).ensure_table("s", "t", _ONE_COL, config=None)
    create_stmt = conn.statements[-1]
    assert create_stmt == 'CREATE TABLE IF NOT EXISTS "iceberg"."s"."t" ("id" INT)'
    assert "WITH" not in create_stmt


def test_ensure_table_partitioning_produces_expected_with_clause():
    """Test that ensure_table threads a validated `partitioning` config into the DDL."""
    conn = _FakeConnection()
    _adapter(conn).ensure_table(
        "s", "t", _ONE_COL, config={"partitioning": ["day(event_ts)"]}
    )
    assert conn.statements[-1] == (
        'CREATE TABLE IF NOT EXISTS "iceberg"."s"."t" ("id" INT) '
        "WITH (partitioning = ARRAY['day(event_ts)'])"
    )


def test_ensure_table_all_three_properties_produce_expected_with_clause():
    """Test that ensure_table renders all three properties in the fixed order."""
    conn = _FakeConnection()
    _adapter(conn).ensure_table(
        "s", "t", _ONE_COL,
        config={
            "partitioning": ["day(event_ts)"],
            "sorted_by": ["id"],
            "format": "PARQUET",
        },
    )
    assert conn.statements[-1] == (
        'CREATE TABLE IF NOT EXISTS "iceberg"."s"."t" ("id" INT) '
        "WITH (partitioning = ARRAY['day(event_ts)'], sorted_by = ARRAY['id'], "
        "format = 'PARQUET')"
    )


@pytest.mark.parametrize(
    ("config", "match"),
    [
        pytest.param({"sortkey": ["id"]}, "sortkey", id="unknown_key"),
        pytest.param({"partitioning": "id"}, "partitioning", id="partitioning_not_a_list"),
        pytest.param({"partitioning": []}, "partitioning", id="partitioning_empty"),
        pytest.param(
            {"partitioning": ["id", 3]}, "partitioning", id="partitioning_non_string_element"
        ),
        pytest.param(
            {"partitioning": ["id", ""]}, "partitioning", id="partitioning_empty_string_element"
        ),
        pytest.param({"sorted_by": "id"}, "sorted_by", id="sorted_by_not_a_list"),
        pytest.param({"sorted_by": []}, "sorted_by", id="sorted_by_empty"),
        pytest.param(
            {"sorted_by": ["id", 3]}, "sorted_by", id="sorted_by_non_string_element"
        ),
        pytest.param(
            {"sorted_by": ["id", ""]}, "sorted_by", id="sorted_by_empty_string_element"
        ),
        pytest.param({"format": 3}, "format", id="format_not_a_string"),
        pytest.param({"format": None}, "format", id="format_none"),
        pytest.param({"format": "AVRO2"}, "format", id="format_not_in_allowlist"),
        pytest.param(
            {"partitioned_by": ["event_ts"]}, "partitioned_by", id="hive_partitioned_by_spelling"
        ),
    ],
)
def test_ensure_table_rejects_bad_config_and_emits_no_ddl(config, match):
    """Test that a rejected config raises ValueError and never touches the connection.

    Config validation is the first thing ensure_table does, before schema
    creation or table creation — so a rejection here means the connection is
    never touched at all: conn.statements stays empty.
    """
    conn = _FakeConnection()
    with pytest.raises(ValueError, match=match):
        _adapter(conn).ensure_table("s", "t", _ONE_COL, config=config)
    assert conn.statements == []


def test_ensure_table_bad_column_type_still_rejects_and_emits_no_ddl():
    """Test that config validation does not skip the pre-existing column-type guard."""
    conn = _FakeConnection()
    with pytest.raises(ValueError):
        _adapter(conn).ensure_table(
            "s", "t", [{"name": "id", "type": "NOT_A_TYPE", "nullable": True}], config=None
        )
    assert conn.statements == []
