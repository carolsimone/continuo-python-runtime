"""Shared fixtures for adapters/duckdb/tests.

This directory is its own top-level pytest package, so it cannot see the root
``tests/conftest.py``. The FakeLakeGateway lives here (not in an importable
module) because two ``tests`` packages cannot both be imported by name under
importlib mode; tests receive it through the ``gateway`` fixture.
"""
import contextlib
import os
import socket
import uuid
from contextlib import suppress

import boto3
import psycopg2
import pyarrow as pa
import pytest

from continuo_duckdb_adapter.adapter import DuckDBAdapter
from continuo_duckdb_adapter.infrastructure.session import DuckLakeSession
from continuo_duckdb_adapter.infrastructure.settings import DuckLakeSettings
from continuo_duckdb_adapter.application.ports import LakeConflictError, LakeGateway
from continuo_duckdb_adapter.application.warehouse import LakeWarehouse


class FakeLakeGateway(LakeGateway):
    """Records every call as ``(method_name, *args)``; can raise on demand."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.existing: set[tuple[str, str]] = set()
        self.conflicts: dict[str, int] = {}
        self.fail_on: dict[str, Exception] = {}
        self.fetch_result = None

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def _do(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if self.conflicts.get(name, 0) > 0:
            self.conflicts[name] -= 1
            raise LakeConflictError(name)
        if name in self.fail_on:
            raise self.fail_on[name]

    @contextlib.contextmanager
    def transaction(self):
        self.calls.append(("begin",))
        try:
            yield
        except BaseException:
            self.calls.append(("rollback",))
            raise
        self.calls.append(("commit",))

    def create_schema_if_not_exists(self, schema):
        self._do("create_schema_if_not_exists", schema)

    def drop_schema_cascade(self, schema):
        self._do("drop_schema_cascade", schema)

    def table_exists(self, table):
        self._do("table_exists", table)
        return (table.schema.name, table.table.name) in self.existing

    def drop_table_if_exists(self, table):
        self._do("drop_table_if_exists", table)

    def create_table(self, table, columns, *, if_not_exists=False):
        self._do("create_table", table, tuple(columns), if_not_exists)

    def create_empty_table_as(self, table, select_sql):
        self._do("create_empty_table_as", table, select_sql)

    def create_empty_clone(self, target, source):
        self._do("create_empty_clone", target, source)

    def apply_layout(self, table, layout):
        self._do("apply_layout", table, layout)

    def explain_read(self, sql):
        self._do("explain_read", sql)

    def fetch_arrow(self, sql):
        self._do("fetch_arrow", sql)
        return self.fetch_result

    def delete_all(self, table):
        self._do("delete_all", table)

    def insert_arrow(self, table, data):
        self._do("insert_arrow", table, data)

    def close(self):
        self._do("close")


class _UnitWarehouse(LakeWarehouse):
    """LakeWarehouse with the composition-root hooks stubbed for unit tests."""

    @classmethod
    def required_env(cls) -> list[str]:
        return []

    @classmethod
    def from_env(cls) -> "_UnitWarehouse":
        raise NotImplementedError


@pytest.fixture
def gateway() -> FakeLakeGateway:
    return FakeLakeGateway()


@pytest.fixture
def warehouse(gateway) -> _UnitWarehouse:
    return _UnitWarehouse(gateway, sleep=lambda _seconds: None)


CATALOG_PORT = int(os.environ.get("VR_IT_DUCKDB_CATALOG_PORT", "15599"))
S3_PORT = int(os.environ.get("VR_IT_DUCKDB_S3_PORT", "19100"))
BUCKET = "warehouse"
DATA_PATH = f"s3://{BUCKET}/lake/"
STACK_HINT = "docker compose -f tests/smoke/duckdb-stack/docker-compose.yml up -d --wait"


def _require_open(port: int) -> None:
    """Fail loudly (never skip) when the compose stack is not running."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return
    except OSError as exc:
        pytest.fail(f"nothing listening on 127.0.0.1:{port}; start the stack: {STACK_HINT} ({exc})")


@pytest.fixture(scope="session")
def lake_env() -> dict[str, str]:
    """DUCKDB_* env for the compose stack; initialises the catalog exactly once.

    DuckLake creates its metadata tables on the first ATTACH, so that must not
    race with the concurrency tests: do it here, once, before any test runs.
    """
    _require_open(CATALOG_PORT)
    _require_open(S3_PORT)
    env = {
        "DUCKDB_CATALOG_HOST": "localhost",
        "DUCKDB_CATALOG_PORT": str(CATALOG_PORT),
        "DUCKDB_CATALOG_DB": "catalog",
        "DUCKDB_CATALOG_USER": "continuo",
        "DUCKDB_CATALOG_PASSWORD": "continuo",
        "DUCKDB_DATA_PATH": DATA_PATH,
        "DUCKDB_S3_ENDPOINT": f"localhost:{S3_PORT}",
        "DUCKDB_S3_ACCESS_KEY_ID": "minioadmin",
        "DUCKDB_S3_SECRET_ACCESS_KEY": "minioadmin",
        "DUCKDB_S3_URL_STYLE": "path",
        "DUCKDB_S3_USE_SSL": "false",
    }
    DuckLakeSession.connect(DuckLakeSettings.from_env(env)).close()
    return env


@pytest.fixture
def adapter_factory(lake_env, monkeypatch):
    """Build real adapters against the stack; all are closed at teardown."""
    made: list[DuckDBAdapter] = []

    def make(**extra_env: str) -> DuckDBAdapter:
        for key, value in {**lake_env, **extra_env}.items():
            monkeypatch.setenv(key, value)
        adapter = DuckDBAdapter.from_env()
        made.append(adapter)
        return adapter

    yield make
    for adapter in made:
        with suppress(Exception):
            adapter.close()


@pytest.fixture
def adapter(adapter_factory) -> DuckDBAdapter:
    return adapter_factory()


@pytest.fixture
def parquet_adapter(adapter_factory) -> DuckDBAdapter:
    """Inlining off: every insert becomes a Parquet file, so layout is observable."""
    return adapter_factory(DUCKDB_DATA_INLINING_ROW_LIMIT="0")


@pytest.fixture
def schema(adapter) -> str:
    name = f"it_{uuid.uuid4().hex[:10]}"
    yield name
    adapter.drop_schema(name)


@pytest.fixture
def prod_table(adapter):
    """A seeded ``(schema, 'src_table')`` with two rows, dropped afterwards."""
    name = f"prod_{uuid.uuid4().hex[:10]}"
    adapter.ensure_table(
        name, "src_table",
        [{"name": "id", "type": "INTEGER", "nullable": True},
         {"name": "name", "type": "VARCHAR(20)", "nullable": True}],
        config={},
    )
    adapter.load(name, "src_table", pa.table({"id": pa.array([1, 2], pa.int32()), "name": ["a", "b"]}))
    yield name, "src_table"
    adapter.drop_schema(name)


@pytest.fixture
def scalar(adapter):
    def run(sql: str):
        table = adapter.fetch(sql)
        return table.to_pylist()[0][table.schema.names[0]]
    return run


@pytest.fixture
def columns_of(adapter):
    def run(schema: str, table: str) -> list[tuple[str, str, str]]:
        rows = adapter.fetch(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            f"WHERE table_catalog = 'lake' AND table_schema = '{schema}' "
            f"AND table_name = '{table}' ORDER BY ordinal_position"
        ).to_pylist()
        return [(r["column_name"], r["data_type"], r["is_nullable"]) for r in rows]
    return run


@pytest.fixture
def tables_in(adapter):
    def run(schema: str) -> list[str]:
        rows = adapter.fetch(
            "SELECT table_name FROM information_schema.tables "
            f"WHERE table_catalog = 'lake' AND table_schema = '{schema}' ORDER BY table_name"
        ).to_pylist()
        return [r["table_name"] for r in rows]
    return run


@pytest.fixture
def catalog_db():
    """A read-only-by-convention cursor on the DuckLake catalog (postgres)."""
    conn = psycopg2.connect(
        host="localhost", port=CATALOG_PORT, dbname="catalog", user="continuo", password="continuo"
    )
    conn.autocommit = True
    yield conn.cursor()
    conn.close()


@pytest.fixture
def s3():
    return boto3.client(
        "s3", endpoint_url=f"http://localhost:{S3_PORT}",
        aws_access_key_id="minioadmin", aws_secret_access_key="minioadmin", region_name="us-east-1",
    )
