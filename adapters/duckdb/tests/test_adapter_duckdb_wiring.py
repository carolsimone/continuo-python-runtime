"""Composition root wiring that needs no running warehouse."""
from importlib.metadata import entry_points

import pytest

from continuo_duckdb_adapter.adapter import DuckDBAdapter


def test_required_env_names_connection_vars():
    assert DuckDBAdapter.required_env() == [
        "DUCKDB_CATALOG_HOST", "DUCKDB_CATALOG_DB", "DUCKDB_CATALOG_USER", "DUCKDB_DATA_PATH",
    ]


def test_entry_point_registered_and_loads_adapter():
    eps = [ep for ep in entry_points(group="continuo_engine.adapters") if ep.name == "duckdb"]
    assert len(eps) == 1
    assert eps[0].load() is DuckDBAdapter


def test_from_env_names_the_missing_variable(monkeypatch):
    for name in DuckDBAdapter.required_env():
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="DUCKDB_CATALOG_HOST"):
        DuckDBAdapter.from_env()
