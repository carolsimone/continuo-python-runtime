"""The engine-contract conformance suite against Trino + Iceberg (tests/smoke/trino-stack).

Run the stack first:
    docker compose -f tests/smoke/trino-stack/docker-compose.yml up -d --wait
"""
import os
import uuid

import pytest

import trino

from continuo_engine_contract.testing.conformance import (
    ARROW,
    COLUMNS,
    AppendConformance,
    ConformanceBase,
    IntrospectionConformance,
    StartupConformance,
    UpsertConformance,
    bulk_rows,
    table_of,
)
from continuo_trino_adapter.adapter import TrinoAdapter

pytestmark = pytest.mark.integration


@pytest.fixture
def adapter():
    conn = trino.dbapi.connect(
        host="localhost",
        port=int(os.environ.get("VR_IT_TRINO_PORT", "18080")),
        user="continuo",
        catalog="iceberg",
        http_scheme="http",
    )
    connected = TrinoAdapter(conn, "iceberg")
    yield connected
    connected.close()


@pytest.fixture
def schema(adapter):
    name = f"conf_{uuid.uuid4().hex[:10]}"
    yield name
    adapter.drop_schema(name)


@pytest.fixture
def warehouse_address():
    return "127.0.0.1", int(os.environ.get("VR_IT_TRINO_PORT", "18080"))


@pytest.fixture
def adapter_from_env(monkeypatch):
    def build(host, port):
        monkeypatch.setenv("TRINO_HOST", host)
        monkeypatch.setenv("TRINO_PORT", str(port))
        monkeypatch.setenv("TRINO_USER", "continuo")
        monkeypatch.setenv("TRINO_CATALOG", "iceberg")
        monkeypatch.setenv("TRINO_HTTP_SCHEME", "http")
        monkeypatch.delenv("TRINO_PASSWORD", raising=False)
        return TrinoAdapter.from_env()

    return build


class TestTrinoStartup(StartupConformance):
    pass


class TestTrinoIntrospection(IntrospectionConformance):
    pass


class TestTrinoAppend(AppendConformance):
    pass


def _stage_tables(adapter, schema):
    rows = adapter._execute(f'SHOW TABLES FROM "iceberg"."{schema}"')
    return [row[0] for row in rows if row[0].startswith("__continuo_stage_")]


def test_a_failed_append_leaves_no_stage_table(adapter, schema):
    table = ConformanceBase().new_table(adapter, schema, COLUMNS)
    with pytest.raises(Exception):
        adapter.append(schema, table, table_of(ARROW, bulk_rows(1)))
    assert _stage_tables(adapter, schema) == []


class TestTrinoUpsert(UpsertConformance):
    pass


def test_a_failed_upsert_leaves_no_stage_table(adapter, schema):
    table = ConformanceBase().new_table(adapter, schema, COLUMNS, primary_key=["id"])
    with pytest.raises(Exception):
        adapter.upsert(schema, table, table_of(ARROW, bulk_rows(1)), ["id"])
    assert _stage_tables(adapter, schema) == []
