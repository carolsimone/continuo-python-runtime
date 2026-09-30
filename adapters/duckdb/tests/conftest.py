"""Shared fixtures for adapters/duckdb/tests.

This directory is its own top-level pytest package, so it cannot see the root
``tests/conftest.py``. The FakeLakeGateway lives here (not in an importable
module) because two ``tests`` packages cannot both be imported by name under
importlib mode; tests receive it through the ``gateway`` fixture.
"""
import contextlib

import pytest

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
