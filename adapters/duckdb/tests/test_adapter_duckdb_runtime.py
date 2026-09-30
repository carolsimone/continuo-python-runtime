"""Runtime use cases, against the recording FakeLakeGateway (no engine)."""
import pyarrow as pa
import pytest

from continuo_duckdb_adapter.application.ports import LakeConflictError
from continuo_duckdb_adapter.domain.columns import ColumnDefinition
from continuo_duckdb_adapter.domain.identifiers import Identifier, QualifiedTable
from continuo_duckdb_adapter.domain.layout import PartitionKey, TableLayout

T = QualifiedTable.of("s", "t")
COLS = [{"name": "id", "type": "INTEGER", "nullable": False}, {"name": "ts", "type": "TIMESTAMP", "nullable": True}]
DEFS = (ColumnDefinition(Identifier("id"), "INTEGER", False), ColumnDefinition(Identifier("ts"), "TIMESTAMP", True))


def test_fetch_returns_the_gateways_arrow_table(warehouse, gateway):
    gateway.fetch_result = pa.table({"a": [1, 2]})
    assert warehouse.fetch("SELECT a FROM s.t") is gateway.fetch_result
    assert gateway.calls == [("fetch_arrow", "SELECT a FROM s.t")]


def test_fetch_rejects_duplicate_output_columns(warehouse, gateway):
    gateway.fetch_result = pa.Table.from_arrays([pa.array([1]), pa.array([2])], names=["id", "id"])
    with pytest.raises(ValueError, match="duplicate column name"):
        warehouse.fetch("SELECT 1 AS id, 2 AS id")


def test_fetch_accepts_an_empty_result(warehouse, gateway):
    gateway.fetch_result = pa.table({"a": pa.array([], pa.int32())})
    assert warehouse.fetch("SELECT a FROM s.t WHERE false").num_rows == 0


def test_validate_config_needs_no_connection(warehouse):
    type(warehouse).validate_config({"partitioned_by": ["id"], "sorted_by": ["id"]}, ["id", "ts"])
    type(warehouse).validate_config(None, ["id"])
    type(warehouse).validate_config({}, ["id"])


@pytest.mark.parametrize("config", [
    {"indexes": []}, {"partitioned_by": ["missing"]}, {"sorted_by": [{"column": "id", "direction": "up"}]},
])
def test_validate_config_rejects_what_ensure_table_rejects(warehouse, config):
    with pytest.raises(ValueError):
        type(warehouse).validate_config(config, ["id"])


def test_validate_config_defers_the_time_transform_type_check(warehouse):
    type(warehouse).validate_config({"partitioned_by": [{"column": "id", "transform": "month"}]}, ["id"])


def test_ensure_table_creates_a_missing_table_once(warehouse, gateway):
    warehouse.ensure_table("s", "t", COLS, config={})
    assert gateway.calls == [
        ("create_schema_if_not_exists", Identifier("s")),
        ("begin",), ("table_exists", T), ("create_table", T, DEFS, False), ("commit",),
    ]


def test_ensure_table_applies_layout_only_when_it_creates_the_table(warehouse, gateway):
    config = {"partitioned_by": [{"column": "ts", "transform": "month"}]}
    layout = TableLayout(partition_keys=(PartitionKey(Identifier("ts"), "month"),))
    warehouse.ensure_table("s", "t", COLS, config=config)
    assert ("apply_layout", T, layout) in gateway.calls
    gateway.calls.clear()
    gateway.existing.add(("s", "t"))
    warehouse.ensure_table("s", "t", COLS, config=config)
    assert gateway.names() == ["create_schema_if_not_exists", "begin", "table_exists", "commit"]


def test_ensure_table_bad_config_or_type_emits_nothing(warehouse, gateway):
    with pytest.raises(ValueError):
        warehouse.ensure_table("s", "t", COLS, config={"nope": 1})
    with pytest.raises(ValueError):
        warehouse.ensure_table("s", "t", [{"name": "id", "type": "INT; DROP"}], config={})
    assert gateway.calls == []


def test_ensure_table_retries_a_creation_conflict(warehouse, gateway):
    gateway.conflicts["create_table"] = 1
    warehouse.ensure_table("s", "t", COLS, config={})
    assert gateway.names().count("create_table") == 2
    assert gateway.names().count("rollback") == 1


def test_ensure_table_gives_up_after_bounded_conflicts(warehouse, gateway):
    gateway.conflicts["create_table"] = 99
    with pytest.raises(LakeConflictError):
        warehouse.ensure_table("s", "t", COLS, config={})
    assert gateway.names().count("create_table") == 5


def test_load_replaces_contents_in_one_transaction(warehouse, gateway):
    data = pa.table({"id": [1, 2]})
    warehouse.load("s", "t", data)
    assert gateway.calls == [("begin",), ("delete_all", T), ("insert_arrow", T, data), ("commit",)]


def test_load_of_zero_rows_only_clears(warehouse, gateway):
    warehouse.load("s", "t", pa.table({"id": pa.array([], pa.int32())}))
    assert gateway.names() == ["begin", "delete_all", "commit"]


def test_load_rolls_back_when_the_insert_fails(warehouse, gateway):
    gateway.fail_on["insert_arrow"] = RuntimeError("NOT NULL violated")
    with pytest.raises(RuntimeError):
        warehouse.load("s", "t", pa.table({"id": [1]}))
    assert gateway.names() == ["begin", "delete_all", "insert_arrow", "rollback"]
