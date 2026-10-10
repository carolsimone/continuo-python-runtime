"""The engine-contract conformance suite against postgres:16 (tests/smoke/postgres-stack).

Run the stack first:
    docker compose -f tests/smoke/postgres-stack/docker-compose.yml up -d --wait
"""
import datetime
import os
import uuid

import psycopg2
import pytest

from continuo_engine_contract.testing.conformance import (
    ARROW,
    COLUMNS,
    FX_COLUMNS,
    NAMES,
    AppendConformance,
    ConformanceBase,
    IntrospectionConformance,
    UpsertConformance,
    table_of,
)
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


class TestPostgresUpsert(UpsertConformance):
    pass


def _primary_key_columns(schema: str, table: str) -> list[str]:
    conn = psycopg2.connect(**PG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT kcu.column_name FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "ON kcu.constraint_name = tc.constraint_name "
                "AND kcu.table_schema = tc.table_schema AND kcu.table_name = tc.table_name "
                "WHERE tc.table_schema = %s AND tc.table_name = %s "
                "AND tc.constraint_type = 'PRIMARY KEY' ORDER BY kcu.ordinal_position",
                (schema, table),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def test_ensure_table_with_a_primary_key_creates_an_enforced_constraint(adapter, schema):
    """Review Focus 4: the key exists as soon as the first run created the table."""
    adapter.ensure_table(schema, "keyed", FX_COLUMNS, config={}, primary_key=["rate_date", "base"])
    assert _primary_key_columns(schema, "keyed") == ["rate_date", "base"]
    conn = psycopg2.connect(**PG)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            insert = f'INSERT INTO "{schema}"."keyed" VALUES (%s, %s, %s)'
            cur.execute(insert, (datetime.date(2026, 10, 8), "EUR", None))
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute(insert, (datetime.date(2026, 10, 8), "EUR", None))
    finally:
        conn.close()


def test_upsert_into_a_table_without_its_primary_key_fails_and_writes_nothing(adapter, schema):
    """ON CONFLICT needs the constraint; postgres refuses instead of collecting duplicates."""
    adapter.ensure_table(schema, "unkeyed", COLUMNS, config={})
    adapter.append(schema, "unkeyed", table_of(ARROW, [(1, "a", 10)]))
    with pytest.raises(psycopg2.errors.InvalidColumnReference):
        adapter.upsert(schema, "unkeyed", table_of(ARROW, [(1, "b", 20)]), ["id"])
    ConformanceBase().expect_rows(adapter, schema, "unkeyed", NAMES, [(1, "a", 10)])
