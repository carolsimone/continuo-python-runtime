"""The engine-contract conformance suite against postgres:16 (tests/smoke/postgres-stack).

Run the stack first:
    docker compose -f tests/smoke/postgres-stack/docker-compose.yml up -d --wait
"""
import os
import uuid

import psycopg2
import pytest

from continuo_engine_contract.conformance import AppendConformance, IntrospectionConformance
from continuo_postgres_adapter.adapter import PostgresAdapter

pytestmark = pytest.mark.integration

PG = dict(
    host="localhost",
    port=os.environ.get("VR_IT_PG_PORT", "15499"),
    dbname="warehouse",
    user="continuo",
    password="continuo",
)


@pytest.fixture
def adapter():
    connected = PostgresAdapter(psycopg2.connect(**PG))
    yield connected
    connected.close()


@pytest.fixture
def schema(adapter):
    name = f"conf_{uuid.uuid4().hex[:10]}"
    yield name
    adapter.drop_schema(name)


class TestPostgresIntrospection(IntrospectionConformance):
    pass


class TestPostgresAppend(AppendConformance):
    pass
