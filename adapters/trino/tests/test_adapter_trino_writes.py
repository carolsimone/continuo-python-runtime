"""Write paths of the trino adapter over a scripted connection: statements in order.

The live behavior (Iceberg commits, NOT NULL in the stage, MERGE semantics) is
covered by the integration suites; this file pins which statements run, in
what order, and what happens when one of them fails.
"""
import re

import pyarrow as pa
import pytest

from continuo_engine_contract.port import LiveColumn
from continuo_trino_adapter.adapter import TrinoAdapter


class _ScriptedCursor:
    def __init__(self, conn: "_ScriptedConnection") -> None:
        self._conn = conn
        self._rows: list = []

    def execute(self, statement, params=None):
        self._conn.statements.append(statement)
        self._conn.params.append(params)
        for marker, error in self._conn.fail_on.items():
            if marker in statement:
                raise error
        self._rows = next(
            (rows for marker, rows in self._conn.rows_for.items() if marker in statement), []
        )

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _ScriptedConnection:
    """Records statements; answers and fails by substring of the statement."""

    def __init__(self, rows_for=None, fail_on=None):
        self.statements: list[str] = []
        self.params: list = []
        self.rows_for = {"$properties": [["s3://warehouse/s/t-0abc"]], **(rows_for or {})}
        self.fail_on = dict(fail_on or {})

    def cursor(self):
        return _ScriptedCursor(self)


def _adapter(conn):
    return TrinoAdapter(conn, "iceberg")


def _ref(statement, kind):
    """The quoted reference of the __continuo_<kind>_<hex> table in *statement*."""
    return re.search(rf'"iceberg"\."s"\."__continuo_{kind}_[0-9a-f]{{32}}"', statement).group(0)


def _name(ref):
    return ref.rsplit(".", 1)[1].strip('"')


LOCATION_QUERY = 'SELECT value FROM "iceberg"."s"."t$properties" WHERE key = \'location\''


def test_load_stages_then_swaps_and_drops_the_old_table():
    conn = _ScriptedConnection()
    _adapter(conn).load("s", "t", pa.table({"id": [1, 2]}))
    s = conn.statements
    stage, old = _ref(s[1], "stage"), _ref(s[3], "old")
    assert s == [
        LOCATION_QUERY,
        f'CREATE TABLE {stage} (LIKE "iceberg"."s"."t" INCLUDING PROPERTIES) '
        f"WITH (location = 's3://warehouse/s/{_name(stage)}')",
        f'INSERT INTO {stage} ("id") VALUES (?), (?)',
        f'ALTER TABLE "iceberg"."s"."t" RENAME TO {old}',
        f'ALTER TABLE {stage} RENAME TO "iceberg"."s"."t"',
        f"DROP TABLE {old}",
    ]


def test_a_failed_stage_insert_on_load_drops_the_stage_and_never_swaps():
    conn = _ScriptedConnection(fail_on={"VALUES (?": RuntimeError("NOT NULL violated")})
    with pytest.raises(RuntimeError, match="NOT NULL"):
        _adapter(conn).load("s", "t", pa.table({"id": [1]}))
    stage = _ref(conn.statements[1], "stage")
    assert conn.statements[-1] == f"DROP TABLE {stage}"
    assert not any("RENAME" in statement for statement in conn.statements)


def test_table_columns_reads_information_schema_with_lowercased_names():
    conn = _ScriptedConnection(rows_for={"information_schema.columns": [["id", "NO"], ["label", "YES"]]})
    columns = _adapter(conn).table_columns("MixedSchema", "MixedTable")
    assert columns == [LiveColumn("id", False), LiveColumn("label", True)]
    assert conn.statements == [
        'SELECT column_name, is_nullable FROM "iceberg".information_schema.columns '
        "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position"
    ]
    assert conn.params == [["mixedschema", "mixedtable"]]


def test_table_columns_of_a_missing_table_raises_lookup_error():
    with pytest.raises(LookupError, match=r"s\.t"):
        _adapter(_ScriptedConnection()).table_columns("s", "t")
