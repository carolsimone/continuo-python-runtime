"""Validation use cases, against the recording FakeLakeGateway (no engine)."""
import pytest

from continuo_duckdb_adapter.application.ports import LakeConflictError
from continuo_duckdb_adapter.domain.columns import ColumnDefinition
from continuo_duckdb_adapter.domain.identifiers import Identifier, QualifiedTable
from continuo_duckdb_adapter.domain.layout import PartitionKey, TableLayout

T = QualifiedTable.of("s", "t")
COLS = [{"name": "id", "type": "INTEGER", "nullable": False}, {"name": "ts", "type": "TIMESTAMP"}]


def test_ensure_schema_creates_the_schema(warehouse, gateway):
    warehouse.ensure_schema("analytics")
    assert gateway.calls == [("create_schema_if_not_exists", Identifier("analytics"))]


def test_ensure_schema_retries_conflicts_then_succeeds(warehouse, gateway):
    gateway.conflicts["create_schema_if_not_exists"] = 2
    warehouse.ensure_schema("analytics")
    assert gateway.names() == ["create_schema_if_not_exists"] * 3


def test_ensure_schema_gives_up_after_bounded_attempts(warehouse, gateway):
    gateway.conflicts["create_schema_if_not_exists"] = 99
    with pytest.raises(LakeConflictError):
        warehouse.ensure_schema("analytics")
    assert gateway.names() == ["create_schema_if_not_exists"] * 5


def test_ensure_schema_does_not_retry_other_errors(warehouse, gateway):
    gateway.fail_on["create_schema_if_not_exists"] = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        warehouse.ensure_schema("analytics")
    assert gateway.names() == ["create_schema_if_not_exists"]


def test_drop_schema_cascades(warehouse, gateway):
    warehouse.drop_schema("analytics")
    assert gateway.calls == [("drop_schema_cascade", Identifier("analytics"))]


def test_build_empty_from_sql_strips_the_terminator_and_rebuilds_in_one_transaction(warehouse, gateway):
    warehouse.build_empty_from_sql("s", "t", "  select 1 as a ;  \n")
    assert gateway.calls == [
        ("begin",),
        ("drop_table_if_exists", T),
        ("create_empty_table_as", T, "select 1 as a"),
        ("commit",),
    ]


def test_build_empty_from_sql_rolls_back_on_failure(warehouse, gateway):
    gateway.fail_on["create_empty_table_as"] = RuntimeError("bind error")
    with pytest.raises(RuntimeError):
        warehouse.build_empty_from_sql("s", "t", "select 1")
    assert gateway.names()[-1] == "rollback"


def test_clone_empty_from_prod_rebuilds_in_one_transaction(warehouse, gateway):
    warehouse.clone_empty_from_prod("cand", "prod", "t")
    assert gateway.calls == [
        ("begin",),
        ("drop_table_if_exists", QualifiedTable.of("cand", "t")),
        ("create_empty_clone", QualifiedTable.of("cand", "t"), QualifiedTable.of("prod", "t")),
        ("commit",),
    ]


def test_build_empty_from_columns_without_config_emits_no_layout(warehouse, gateway):
    warehouse.build_empty_from_columns("s", "t", COLS, {})
    assert gateway.names() == ["begin", "drop_table_if_exists", "create_table", "commit"]
    assert gateway.calls[2] == (
        "create_table", T,
        (ColumnDefinition(Identifier("id"), "INTEGER", False),
         ColumnDefinition(Identifier("ts"), "TIMESTAMP", True)),
        False,
    )


def test_build_empty_from_columns_applies_layout_inside_the_transaction(warehouse, gateway):
    warehouse.build_empty_from_columns("s", "t", COLS, {"partitioned_by": [{"column": "ts", "transform": "month"}]})
    assert gateway.names() == ["begin", "drop_table_if_exists", "create_table", "apply_layout", "commit"]
    assert gateway.calls[3] == (
        "apply_layout", T, TableLayout(partition_keys=(PartitionKey(Identifier("ts"), "month"),)),
    )


@pytest.mark.parametrize("config", [
    {"sortkey": ["id"]},
    {"partitioned_by": ["missing"]},
    {"partitioned_by": [{"column": "id", "transform": "month"}]},
    {"sorted_by": "id"},
])
def test_bad_config_is_rejected_before_any_statement(warehouse, gateway, config):
    with pytest.raises(ValueError):
        warehouse.build_empty_from_columns("s", "t", COLS, config)
    assert gateway.calls == []


def test_bad_column_type_is_rejected_before_any_statement(warehouse, gateway):
    with pytest.raises(ValueError):
        warehouse.build_empty_from_columns("s", "t", [{"name": "id", "type": "INT; DROP TABLE x"}], {})
    assert gateway.calls == []


def test_check_binds_runs_the_gate_before_touching_the_gateway(warehouse, gateway):
    for bad in ("SELECT 1; DROP TABLE t", "DELETE FROM t", "SELECT 1) AS x; DROP TABLE t; SELECT * FROM (SELECT 1"):
        with pytest.raises(ValueError):
            warehouse.check_binds(bad)
    assert gateway.calls == []


@pytest.mark.parametrize("sql,inner", [
    ("SELECT id FROM s.t;", "SELECT id FROM s.t"),
    ("SELECT ';' AS semi", "SELECT ';' AS semi"),
    ("SELECT 1 -- trailing comment", "SELECT 1 -- trailing comment"),
    ("WITH c AS (SELECT 1 AS a) SELECT a FROM c", "WITH c AS (SELECT 1 AS a) SELECT a FROM c"),
    ("VALUES (1), (2)", "VALUES (1), (2)"),
])
def test_check_binds_passes_a_single_read_to_explain(warehouse, gateway, sql, inner):
    warehouse.check_binds(sql)
    assert gateway.calls == [("explain_read", inner)]


def test_close_closes_the_gateway(warehouse, gateway):
    warehouse.close()
    assert gateway.calls == [("close",)]
