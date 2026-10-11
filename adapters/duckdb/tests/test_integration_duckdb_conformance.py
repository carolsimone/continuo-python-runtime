"""The engine-contract conformance suite against a real DuckLake (tests/smoke/duckdb-stack).

The `adapter` and `schema` fixtures come from this directory's conftest.py.
Run the stack first:
    docker compose -f tests/smoke/duckdb-stack/docker-compose.yml up -d --wait
"""
import pytest

from continuo_engine_contract.testing.conformance import (
    AppendConformance,
    IntrospectionConformance,
    UpsertConformance,
)

pytestmark = pytest.mark.integration


class TestDuckDBIntrospection(IntrospectionConformance):
    pass


class TestDuckDBAppend(AppendConformance):
    pass


class TestDuckDBUpsert(UpsertConformance):
    pass
