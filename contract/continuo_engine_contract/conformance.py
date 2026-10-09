"""Conformance tests for the engine contract's write capabilities.

One test class per capability. An adapter's integration suite subclasses the
classes for exactly the capabilities it declares and runs them against its
real engine; a bring-your-own adapter can do the same::

    from continuo_engine_contract.conformance import AppendConformance

    class TestMyEngineAppend(AppendConformance):
        pass

The subclass's module (or a conftest.py above it) provides two fixtures:

``adapter``
    A connected adapter instance that declares the capability under test.
``schema``
    A schema name the tests may create tables in (``ensure_table`` creates
    it). The fixture drops it after the test.

Rows are read back through ``adapter.fetch`` with ANSI double-quoted
identifiers; an engine that quotes differently overrides
:meth:`ConformanceBase.read_rows`. Comparisons raise ``AssertionError`` with
both sides in the message, so no pytest assertion rewriting is needed.

Needs the ``testing`` extra: ``pip install "continuo-engine-contract[testing]"``.
"""
from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest

from continuo_engine_contract.keys import DuplicateKeyError
from continuo_engine_contract.port import LiveColumn

# More rows than one insert statement carries in any shipped adapter (postgres
# pages 1000 rows, trino batches 500), so an atomicity case spans statements.
BULK_ROWS = 1200

COLUMNS = [
    {"name": "id", "type": "BIGINT", "nullable": False},
    {"name": "name", "type": "VARCHAR(20)", "nullable": False},
    {"name": "score", "type": "BIGINT", "nullable": True},
]
NAMES = ("id", "name", "score")
ARROW = pa.schema([("id", pa.int64()), ("name", pa.string()), ("score", pa.int64())])

# Every column in the key: an upsert has nothing to overwrite.
KEY_ONLY_COLUMNS = [
    {"name": "a", "type": "BIGINT", "nullable": False},
    {"name": "b", "type": "VARCHAR(20)", "nullable": False},
]
KEY_ONLY_ARROW = pa.schema([("a", pa.int64()), ("b", pa.string())])

# Names that need quoting: a reserved word, mixed case, a percent sign.
AWKWARD_COLUMNS = [
    {"name": "order", "type": "BIGINT", "nullable": False},
    {"name": "Rate", "type": "VARCHAR(20)", "nullable": False},
    {"name": "pct%", "type": "BIGINT", "nullable": True},
]
AWKWARD_NAMES = ("order", "Rate", "pct%")
AWKWARD_ARROW = pa.schema([("order", pa.int64()), ("Rate", pa.string()), ("pct%", pa.int64())])

# The shape of the first upsert node (daily FX rates).
FX_COLUMNS = [
    {"name": "rate_date", "type": "DATE", "nullable": False},
    {"name": "base", "type": "VARCHAR(3)", "nullable": False},
    {"name": "rate", "type": "DECIMAL(12,6)", "nullable": True},
]
FX_NAMES = ("rate_date", "base", "rate")
FX_ARROW = pa.schema(
    [("rate_date", pa.date32()), ("base", pa.string()), ("rate", pa.decimal128(12, 6))]
)


def quote(name: str) -> str:
    """*name* as an ANSI double-quoted identifier."""
    return '"' + name.replace('"', '""') + '"'


def table_of(schema: Any, rows: Sequence[Sequence[Any]]) -> Any:
    """An Arrow table of *rows* (tuples in *schema*'s column order)."""
    return pa.Table.from_pylist([dict(zip(schema.names, row)) for row in rows], schema=schema)


def bulk_rows(start: int) -> list[tuple[Any, ...]]:
    """BULK_ROWS valid ``ARROW`` rows with ids from *start*, then one whose NOT NULL name is NULL."""
    rows: list[tuple[Any, ...]] = [(i, f"n{i}", i) for i in range(start, start + BULK_ROWS)]
    rows.append((start + BULK_ROWS, None, 0))
    return rows


def canonical(rows: Iterable[Sequence[Any]]) -> list[tuple[Any, ...]]:
    """*rows* as tuples in a deterministic order, for comparing a table's contents."""
    return sorted((tuple(row) for row in rows), key=repr)


class ConformanceBase:
    """Helpers shared by the capability classes (fixtures: see the module docstring)."""

    def new_table(
        self,
        adapter: Any,
        schema: str,
        columns: list[dict[str, Any]],
        primary_key: Sequence[str] = (),
    ) -> str:
        """Create a fresh table through ``ensure_table`` and return its name."""
        table = f"conf_{uuid.uuid4().hex[:12]}"
        if primary_key:
            adapter.ensure_table(schema, table, columns, config={}, primary_key=primary_key)
        else:
            adapter.ensure_table(schema, table, columns, config={})
        return table

    def read_rows(
        self, adapter: Any, schema: str, table: str, names: Sequence[str]
    ) -> list[tuple[Any, ...]]:
        """Every row of ``schema.table`` as a tuple in *names* order, canonically sorted."""
        select = ", ".join(f"{quote(name)} AS c{index}" for index, name in enumerate(names))
        data = adapter.fetch(f"SELECT {select} FROM {quote(schema)}.{quote(table)}")
        return canonical(
            tuple(row[f"c{index}"] for index in range(len(names))) for row in data.to_pylist()
        )

    def expect_rows(
        self,
        adapter: Any,
        schema: str,
        table: str,
        names: Sequence[str],
        expected: Iterable[Sequence[Any]],
    ) -> None:
        """Fail unless ``schema.table`` holds exactly *expected*, in any order."""
        actual = self.read_rows(adapter, schema, table, names)
        wanted = canonical(expected)
        if actual != wanted:
            raise AssertionError(f"{schema}.{table} holds {actual}, expected {wanted}")


class IntrospectionConformance(ConformanceBase):
    """For an adapter that is a ``TableIntrospection`` (every write capability is)."""

    def test_table_columns_reports_names_and_nullability(self, adapter, schema):
        table = self.new_table(adapter, schema, COLUMNS)
        reported = sorted(adapter.table_columns(schema, table), key=lambda column: column.name)
        expected = [LiveColumn("id", False), LiveColumn("name", False), LiveColumn("score", True)]
        if reported != expected:
            raise AssertionError(f"table_columns reported {reported}, expected {expected}")

    def test_table_columns_of_a_missing_table_raises_lookup_error(self, adapter, schema):
        self.new_table(adapter, schema, COLUMNS)  # the schema exists; only the table is missing
        with pytest.raises(LookupError):
            adapter.table_columns(schema, f"conf_missing_{uuid.uuid4().hex[:8]}")


class AppendConformance(ConformanceBase):
    """For an adapter that is an ``AppendCapable``."""

    def test_append_into_an_empty_table(self, adapter, schema):
        table = self.new_table(adapter, schema, COLUMNS)
        adapter.append(schema, table, table_of(ARROW, [(1, "a", 10), (2, "b", None)]))
        self.expect_rows(adapter, schema, table, NAMES, [(1, "a", 10), (2, "b", None)])

    def test_two_appends_accumulate(self, adapter, schema):
        table = self.new_table(adapter, schema, COLUMNS)
        adapter.append(schema, table, table_of(ARROW, [(1, "a", 10)]))
        adapter.append(schema, table, table_of(ARROW, [(2, "b", 20), (3, "c", 30)]))
        self.expect_rows(
            adapter, schema, table, NAMES, [(1, "a", 10), (2, "b", 20), (3, "c", 30)]
        )

    def test_append_of_zero_rows_writes_nothing(self, adapter, schema):
        table = self.new_table(adapter, schema, COLUMNS)
        adapter.append(schema, table, table_of(ARROW, [(1, "a", 10)]))
        adapter.append(schema, table, table_of(ARROW, []))
        self.expect_rows(adapter, schema, table, NAMES, [(1, "a", 10)])

    def test_append_is_all_rows_or_none(self, adapter, schema):
        """More rows than one insert statement carries; the last violates NOT NULL."""
        table = self.new_table(adapter, schema, COLUMNS)
        adapter.append(schema, table, table_of(ARROW, [(1, "a", 10)]))
        with pytest.raises(Exception):
            adapter.append(schema, table, table_of(ARROW, bulk_rows(100)))
        self.expect_rows(adapter, schema, table, NAMES, [(1, "a", 10)])

    def test_append_names_its_columns(self, adapter, schema):
        """The data's column order need not match the table's."""
        table = self.new_table(adapter, schema, COLUMNS)
        reordered = pa.table({
            "score": pa.array([10], pa.int64()),
            "name": pa.array(["a"], pa.string()),
            "id": pa.array([1], pa.int64()),
        })
        adapter.append(schema, table, reordered)
        self.expect_rows(adapter, schema, table, NAMES, [(1, "a", 10)])

    def test_append_quotes_awkward_identifiers(self, adapter, schema):
        table = self.new_table(adapter, schema, AWKWARD_COLUMNS)
        adapter.append(schema, table, table_of(AWKWARD_ARROW, [(1, "x", 5), (2, "y", None)]))
        self.expect_rows(adapter, schema, table, AWKWARD_NAMES, [(1, "x", 5), (2, "y", None)])


class UpsertConformance(ConformanceBase):
    """For an adapter that is an ``UpsertCapable``. Tables are made by ``ensure_table`` with the key."""

    def test_upsert_inserts_new_keys_into_a_table_created_in_the_same_run(self, adapter, schema):
        table = self.new_table(adapter, schema, COLUMNS, primary_key=["id"])
        adapter.upsert(schema, table, table_of(ARROW, [(1, "a", 10), (2, "b", None)]), ["id"])
        self.expect_rows(adapter, schema, table, NAMES, [(1, "a", 10), (2, "b", None)])

    def test_upsert_overwrites_existing_keys_and_leaves_other_rows_untouched(self, adapter, schema):
        table = self.new_table(adapter, schema, COLUMNS, primary_key=["id"])
        adapter.upsert(
            schema, table, table_of(ARROW, [(1, "a", 10), (2, "b", 20), (3, "c", 30)]), ["id"]
        )
        adapter.upsert(schema, table, table_of(ARROW, [(2, "B", None), (4, "d", 40)]), ["id"])
        self.expect_rows(
            adapter, schema, table, NAMES,
            [(1, "a", 10), (2, "B", None), (3, "c", 30), (4, "d", 40)],
        )

    def test_upsert_rerun_is_idempotent(self, adapter, schema):
        table = self.new_table(adapter, schema, COLUMNS, primary_key=["id"])
        rows = [(1, "a", 10), (2, "b", 20)]
        adapter.upsert(schema, table, table_of(ARROW, rows), ["id"])
        adapter.upsert(schema, table, table_of(ARROW, rows), ["id"])
        self.expect_rows(adapter, schema, table, NAMES, rows)

    def test_upsert_with_a_repeated_key_raises_and_writes_nothing(self, adapter, schema):
        table = self.new_table(adapter, schema, COLUMNS, primary_key=["id"])
        adapter.upsert(schema, table, table_of(ARROW, [(1, "a", 10)]), ["id"])
        with pytest.raises(DuplicateKeyError):
            adapter.upsert(
                schema, table, table_of(ARROW, [(1, "x", 1), (5, "y", 1), (5, "z", 2)]), ["id"]
            )
        self.expect_rows(adapter, schema, table, NAMES, [(1, "a", 10)])

    def test_upsert_when_every_column_is_in_the_key(self, adapter, schema):
        table = self.new_table(adapter, schema, KEY_ONLY_COLUMNS, primary_key=["a", "b"])
        adapter.upsert(schema, table, table_of(KEY_ONLY_ARROW, [(1, "x"), (1, "y")]), ["a", "b"])
        adapter.upsert(schema, table, table_of(KEY_ONLY_ARROW, [(1, "x"), (2, "x")]), ["a", "b"])
        self.expect_rows(adapter, schema, table, ("a", "b"), [(1, "x"), (1, "y"), (2, "x")])

    def test_upsert_is_all_rows_or_none(self, adapter, schema):
        """Updates of existing keys plus more rows than one statement; the last violates NOT NULL."""
        table = self.new_table(adapter, schema, COLUMNS, primary_key=["id"])
        adapter.upsert(schema, table, table_of(ARROW, [(1, "a", 10), (2, "b", 20)]), ["id"])
        rows = [(1, "changed", 1), (2, "changed", 2)] + bulk_rows(100)
        with pytest.raises(Exception):
            adapter.upsert(schema, table, table_of(ARROW, rows), ["id"])
        self.expect_rows(adapter, schema, table, NAMES, [(1, "a", 10), (2, "b", 20)])

    def test_upsert_of_zero_rows_writes_nothing(self, adapter, schema):
        table = self.new_table(adapter, schema, COLUMNS, primary_key=["id"])
        adapter.upsert(schema, table, table_of(ARROW, [(1, "a", 10)]), ["id"])
        adapter.upsert(schema, table, table_of(ARROW, []), ["id"])
        self.expect_rows(adapter, schema, table, NAMES, [(1, "a", 10)])

    def test_upsert_names_its_columns(self, adapter, schema):
        table = self.new_table(adapter, schema, COLUMNS, primary_key=["id"])
        adapter.upsert(schema, table, table_of(ARROW, [(1, "a", 10)]), ["id"])
        reordered = pa.table({
            "score": pa.array([11, 20], pa.int64()),
            "name": pa.array(["A", "b"], pa.string()),
            "id": pa.array([1, 2], pa.int64()),
        })
        adapter.upsert(schema, table, reordered, ["id"])
        self.expect_rows(adapter, schema, table, NAMES, [(1, "A", 11), (2, "b", 20)])

    def test_upsert_quotes_awkward_identifiers(self, adapter, schema):
        key = ["order", "Rate"]
        table = self.new_table(adapter, schema, AWKWARD_COLUMNS, primary_key=key)
        adapter.upsert(schema, table, table_of(AWKWARD_ARROW, [(1, "x", 5), (1, "y", 6)]), key)
        adapter.upsert(schema, table, table_of(AWKWARD_ARROW, [(1, "x", 50), (2, "x", None)]), key)
        self.expect_rows(
            adapter, schema, table, AWKWARD_NAMES, [(1, "x", 50), (1, "y", 6), (2, "x", None)]
        )

    def test_upsert_with_a_date_and_text_key_and_a_decimal_value(self, adapter, schema):
        key = ["rate_date", "base"]
        day1, day2 = datetime.date(2026, 10, 8), datetime.date(2026, 10, 9)
        table = self.new_table(adapter, schema, FX_COLUMNS, primary_key=key)
        adapter.upsert(
            schema, table,
            table_of(FX_ARROW, [(day1, "EUR", Decimal("1.081200")), (day1, "GBP", Decimal("0.861000"))]),
            key,
        )
        adapter.upsert(
            schema, table,
            table_of(FX_ARROW, [(day1, "EUR", Decimal("1.090000")), (day2, "EUR", None)]),
            key,
        )
        self.expect_rows(
            adapter, schema, table, FX_NAMES,
            [
                (day1, "EUR", Decimal("1.090000")),
                (day1, "GBP", Decimal("0.861000")),
                (day2, "EUR", None),
            ],
        )
