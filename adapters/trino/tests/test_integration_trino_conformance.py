"""The engine-contract conformance suite against Trino + Iceberg (tests/smoke/trino-stack).

Run the stack first:
    docker compose -f tests/smoke/trino-stack/docker-compose.yml up -d --wait
"""
import os
import uuid

import pytest

import trino

from continuo_engine_contract.conformance import (
    ARROW,
    COLUMNS,
    AppendConformance,
    ConformanceBase,
    IntrospectionConformance,
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
