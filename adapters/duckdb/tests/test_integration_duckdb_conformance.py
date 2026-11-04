"""The engine-contract conformance suite against a real DuckLake (tests/smoke/duckdb-stack).

The `adapter`, `schema` and `lake_env` fixtures come from this directory's
conftest.py. The first connection ``from_env`` opens is the catalog's, so the
startup case puts the catalog behind the refusing port.
Run the stack first:
    docker compose -f tests/smoke/duckdb-stack/docker-compose.yml up -d --wait
"""
import pytest

from continuo_engine_contract.testing.conformance import (
    AppendConformance,
    IntrospectionConformance,
    StartupConformance,
    UpsertConformance,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def warehouse_address(lake_env):
    return "127.0.0.1", int(lake_env["DUCKDB_CATALOG_PORT"])


@pytest.fixture
def adapter_from_env(lake_env, monkeypatch):
    from continuo_duckdb_adapter.adapter import DuckDBAdapter

    def build(host, port):
        for key, value in {**lake_env, "DUCKDB_CATALOG_HOST": host, "DUCKDB_CATALOG_PORT": str(port)}.items():
            monkeypatch.setenv(key, value)
        return DuckDBAdapter.from_env()

    return build


class TestDuckDBStartup(StartupConformance):
    pass


class TestDuckDBIntrospection(IntrospectionConformance):
    pass


class TestDuckDBAppend(AppendConformance):
    pass


class TestDuckDBUpsert(UpsertConformance):
    pass
