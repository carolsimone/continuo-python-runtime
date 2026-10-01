"""ColumnDefinition: contract type grammar, nullability default, temporal detection."""
import pytest

from continuo_duckdb_adapter.domain.columns import (
    ColumnDefinition,
    column_types,
    is_temporal_type,
)
from continuo_duckdb_adapter.domain.identifiers import Identifier


@pytest.mark.parametrize(
    "type_str",
    ["BIGINT", "INT", "INTEGER", "DOUBLE PRECISION", "TEXT", "TIMESTAMP", "DATE", "BOOLEAN",
     "NUMERIC(10,2)", "NUMERIC(10, 2)", "DECIMAL(5,0)", "VARCHAR(255)", "CHAR(1)", "bigint"],
)
def test_accepts_the_contract_grammar(type_str):
    assert ColumnDefinition(Identifier("c"), type_str).type == type_str


@pytest.mark.parametrize(
    "bad", ["INTEGER; DROP TABLE x", "JSON", "VARCHAR", "INT\n", "NUMERIC(１,2)", ""]
)
def test_rejects_anything_outside_the_grammar(bad):
    with pytest.raises(ValueError):
        ColumnDefinition(Identifier("c"), bad)


def test_from_mapping_defaults_nullable_to_true():
    col = ColumnDefinition.from_mapping({"name": "id", "type": "INTEGER"})
    assert col == ColumnDefinition(Identifier("id"), "INTEGER", True)


def test_from_mapping_honours_nullable_false():
    assert ColumnDefinition.from_mapping({"name": "id", "type": "INT", "nullable": False}).nullable is False


@pytest.mark.parametrize("raw", [{}, {"name": "id"}, {"type": "INT"}, "id", None])
def test_from_mapping_rejects_malformed_entries(raw):
    with pytest.raises(ValueError):
        ColumnDefinition.from_mapping(raw)


@pytest.mark.parametrize("type_str,expected", [
    ("TIMESTAMP", True), ("timestamp", True), ("DATE", True),
    ("TEXT", False), ("BIGINT", False), ("VARCHAR(3)", False),
])
def test_temporal_detection(type_str, expected):
    assert is_temporal_type(type_str) is expected


def test_column_types_maps_name_to_type():
    cols = [ColumnDefinition.from_mapping({"name": "a", "type": "DATE"}),
            ColumnDefinition.from_mapping({"name": "b", "type": "TEXT"})]
    assert column_types(cols) == {"a": "DATE", "b": "TEXT"}
