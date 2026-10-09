"""The engine-contract conformance suite against Trino + Iceberg (tests/smoke/trino-stack).

Run the stack first:
    docker compose -f tests/smoke/trino-stack/docker-compose.yml up -d --wait
"""
import os
import uuid

import pytest

import trino

from continuo_engine_contract.conformance import IntrospectionConformance
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
