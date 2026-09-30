"""The adapter connects to the real DuckLake stack, and fails cleanly when it cannot."""
import duckdb
import pytest

pytestmark = pytest.mark.integration


def test_from_env_connects_and_serves_a_read(adapter):
    assert adapter.fetch("SELECT 41 + 1 AS answer").to_pylist() == [{"answer": 42}]


def test_ensure_schema_creates_a_schema_visible_to_another_connection(adapter, adapter_factory, schema):
    adapter.ensure_schema(schema)
    other = adapter_factory()
    other.ensure_table(schema, "t", [{"name": "id", "type": "INTEGER", "nullable": True}], config={})
    assert other.fetch(f'SELECT count(*) AS n FROM "{schema}"."t"').to_pylist() == [{"n": 0}]


def test_unreachable_catalog_raises_a_clear_error(adapter_factory):
    with pytest.raises(duckdb.Error):
        adapter_factory(DUCKDB_CATALOG_PORT="1")


def test_wrong_catalog_password_raises_a_clear_error(adapter_factory):
    with pytest.raises(duckdb.Error):
        adapter_factory(DUCKDB_CATALOG_PASSWORD="definitely-wrong")
