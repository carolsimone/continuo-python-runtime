"""The conformance suite, run against an in-memory reference adapter.

Proves the suite's own cases agree with each other before any engine runs them,
shows a bring-your-own adapter how to use it, and checks with two broken
adapters that the suite really catches a non-atomic append and an upsert that
loses rows.
"""
import pytest

from continuo_engine_contract.port import (
    AppendCapable,
    LiveColumn,
    UpsertCapable,
    WarehouseAdapter,
)
from continuo_engine_contract.testing.conformance import (
    AppendConformance,
    IntrospectionConformance,
    UpsertConformance,
    canonical,
)


class _MemoryWarehouse(WarehouseAdapter, AppendCapable, UpsertCapable):
    """A reference adapter over Python lists: NOT NULL enforced, every write all-or-nothing."""

    def __init__(self):
        self.tables: dict[tuple[str, str], dict] = {}

    @classmethod
    def required_env(cls):
        return []

    @classmethod
    def from_env(cls):
        return cls()

    def ensure_schema(self, schema):
        ...

    def drop_schema(self, schema):
        for key in [key for key in self.tables if key[0] == schema]:
            del self.tables[key]

    def build_empty_from_sql(self, schema, table, compiled_sql):
        ...

    def clone_empty_from_prod(self, candidate_schema, prod_schema, table):
        ...

    def build_empty_from_columns(self, schema, table, columns, config):
        ...

    def check_binds(self, sql):
        ...

    def fetch(self, sql):
        raise AssertionError("the memory adapter is read through read_rows, never fetch")

    def ensure_table(self, schema, table, columns, *, config, primary_key=()):
        self.tables.setdefault(
            (schema, table), {"columns": [dict(c) for c in columns], "rows": []}
        )

    def load(self, schema, table, data):
        self.tables[(schema, table)]["rows"] = self._rows(schema, table, data.to_pylist())

    def close(self):
        ...

    def table_columns(self, schema, table):
        if (schema, table) not in self.tables:
            raise LookupError(f"table {schema}.{table} does not exist")
        return [
            LiveColumn(c["name"], c.get("nullable", True))
            for c in self.tables[(schema, table)]["columns"]
        ]

    def _rows(self, schema, table, rows):
        """*rows* projected onto the table's columns; ValueError on a NULL in a NOT NULL column."""
        columns = self.tables[(schema, table)]["columns"]
        for row in rows:
            for column in columns:
                if not column.get("nullable", True) and row.get(column["name"]) is None:
                    raise ValueError(f"NOT NULL violated: {column['name']}")
        return [{c["name"]: row.get(c["name"]) for c in columns} for row in rows]

    def append(self, schema, table, data):
        rows = self._rows(schema, table, data.to_pylist())  # validate everything first
        self.tables[(schema, table)]["rows"].extend(rows)

    def _upsert(self, schema, table, data, primary_key):
        rows = self._rows(schema, table, data.to_pylist())
        stored = self.tables[(schema, table)]["rows"]
        index = {tuple(row[k] for k in primary_key): i for i, row in enumerate(stored)}
        for row in rows:
            key = tuple(row[k] for k in primary_key)
            if key in index:
                stored[index[key]] = row
            else:
                index[key] = len(stored)
                stored.append(row)


class _LeakyAppendWarehouse(_MemoryWarehouse):
    """Appends row by row, so a failing row leaves the earlier ones behind."""

    def append(self, schema, table, data):
        for row in data.to_pylist():
            self.tables[(schema, table)]["rows"].extend(self._rows(schema, table, [row]))


class _ReplacingUpsertWarehouse(_MemoryWarehouse):
    """Upserts by replacing the whole table, losing rows whose key is not in the data."""

    def _upsert(self, schema, table, data, primary_key):
        self.tables[(schema, table)]["rows"] = self._rows(schema, table, data.to_pylist())


class _ReadsMemory:
    """Reads the memory adapter's rows directly instead of through fetch."""

    def read_rows(self, adapter, schema, table, names):
        rows = adapter.tables[(schema, table)]["rows"]
        return canonical(tuple(row[name] for name in names) for row in rows)


@pytest.fixture
def adapter():
    return _MemoryWarehouse()


@pytest.fixture
def schema(adapter):
    yield "conf"
    adapter.drop_schema("conf")


class TestMemoryIntrospection(_ReadsMemory, IntrospectionConformance):
    pass


class TestMemoryAppend(_ReadsMemory, AppendConformance):
    pass


class TestMemoryUpsert(_ReadsMemory, UpsertConformance):
    pass


class _MemoryAppendCase(_ReadsMemory, AppendConformance):
    pass


class _MemoryUpsertCase(_ReadsMemory, UpsertConformance):
    pass


def test_the_suite_catches_a_non_atomic_append():
    with pytest.raises(AssertionError, match="holds"):
        _MemoryAppendCase().test_append_is_all_rows_or_none(_LeakyAppendWarehouse(), "conf")


def test_the_suite_catches_an_upsert_that_loses_other_rows():
    with pytest.raises(AssertionError, match="holds"):
        _MemoryUpsertCase().test_upsert_overwrites_existing_keys_and_leaves_other_rows_untouched(
            _ReplacingUpsertWarehouse(), "conf"
        )
