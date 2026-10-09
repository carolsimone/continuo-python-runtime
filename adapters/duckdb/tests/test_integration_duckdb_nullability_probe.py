"""Probe (spec section 8): DuckLake reports NOT NULL through introspection.

table_columns reads duckdb_columns(); the run-time shape check compares the
nullability it reports with the contract. Pinned against the compose stack's
DuckLake (postgres catalog, MinIO data).
"""
import pytest

pytestmark = pytest.mark.integration

COLS = [
    {"name": "id", "type": "BIGINT", "nullable": False},
    {"name": "label", "type": "VARCHAR(10)", "nullable": True},
]


def test_duckdb_columns_reports_not_null_on_ducklake(adapter, schema):
    adapter.ensure_table(schema, "probe", COLS, config={})
    rows = adapter.fetch(
        "SELECT column_name, is_nullable FROM duckdb_columns() "
        f"WHERE database_name = 'lake' AND schema_name = '{schema}' AND table_name = 'probe' "
        "ORDER BY column_index"
    ).to_pylist()
    assert rows == [
        {"column_name": "id", "is_nullable": False},
        {"column_name": "label", "is_nullable": True},
    ]


def test_information_schema_agrees(adapter, schema, columns_of):
    adapter.ensure_table(schema, "probe", COLS, config={})
    assert [(name, nullable) for name, _type, nullable in columns_of(schema, "probe")] == [
        ("id", "NO"),
        ("label", "YES"),
    ]
