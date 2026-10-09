"""Write paths of the trino adapter over a scripted connection: statements in order.

The live behavior (Iceberg commits, NOT NULL in the stage, MERGE semantics) is
covered by the integration suites; this file pins which statements run, in
what order, and what happens when one of them fails.
"""
import logging
import re

import pyarrow as pa
import pytest

from continuo_engine_contract.keys import DuplicateKeyError
from continuo_engine_contract.port import LiveColumn
import continuo_trino_adapter.adapter as adapter_module
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


def test_append_statement_names_and_quotes_every_column():
    assert adapter_module._append_statement(
        '"iceberg"."s"."t"', '"iceberg"."s"."st"', ["order", "Rate", "pct%"]
    ) == (
        'INSERT INTO "iceberg"."s"."t" ("order", "Rate", "pct%") '
        'SELECT "order", "Rate", "pct%" FROM "iceberg"."s"."st"'
    )


def test_append_stages_then_inserts_once_then_drops_the_stage():
    conn = _ScriptedConnection()
    _adapter(conn).append("s", "t", pa.table({"id": [1, 2]}))
    s = conn.statements
    stage = _ref(s[1], "stage")
    assert s == [
        LOCATION_QUERY,
        f'CREATE TABLE {stage} (LIKE "iceberg"."s"."t" INCLUDING PROPERTIES) '
        f"WITH (location = 's3://warehouse/s/{_name(stage)}')",
        f'INSERT INTO {stage} ("id") VALUES (?), (?)',
        f'INSERT INTO "iceberg"."s"."t" ("id") SELECT "id" FROM {stage}',
        f"DROP TABLE {stage}",
    ]


def test_append_of_zero_rows_creates_no_stage():
    conn = _ScriptedConnection()
    _adapter(conn).append("s", "t", pa.table({"id": pa.array([], pa.int64())}))
    assert conn.statements == []


def test_a_failed_stage_insert_never_touches_the_target_and_drops_the_stage():
    conn = _ScriptedConnection(fail_on={"VALUES (?": RuntimeError("NOT NULL violated")})
    with pytest.raises(RuntimeError, match="NOT NULL"):
        _adapter(conn).append("s", "t", pa.table({"id": [1]}))
    stage = _ref(conn.statements[1], "stage")
    assert not any(st.startswith('INSERT INTO "iceberg"."s"."t"') for st in conn.statements)
    assert conn.statements[-1] == f"DROP TABLE {stage}"


def test_a_failed_stage_drop_after_the_commit_is_only_logged(caplog):
    conn = _ScriptedConnection(
        fail_on={'DROP TABLE "iceberg"."s"."__continuo_stage_': RuntimeError("catalog hiccup")}
    )
    with caplog.at_level(logging.WARNING, logger="continuo_trino_adapter"):
        _adapter(conn).append("s", "t", pa.table({"id": [1]}))
    assert any(st.startswith('INSERT INTO "iceberg"."s"."t"') for st in conn.statements)
    assert "best-effort cleanup of staging table" in caplog.text


_FX = [
    {"name": "rate_date", "type": "DATE", "nullable": False},
    {"name": "base", "type": "VARCHAR(3)", "nullable": False},
]


def test_ensure_table_with_a_key_emits_the_same_ddl_as_without():
    keyed, plain = _ScriptedConnection(), _ScriptedConnection()
    _adapter(keyed).ensure_table("s", "t", _FX, config={}, primary_key=("rate_date", "base"))
    _adapter(plain).ensure_table("s", "t", _FX, config={})
    assert keyed.statements == plain.statements


def test_an_upsert_key_on_a_format_version_1_table_is_rejected_before_any_ddl():
    conn = _ScriptedConnection()
    with pytest.raises(ValueError, match="format_version"):
        _adapter(conn).ensure_table(
            "s", "t", _FX, config={"format_version": 1}, primary_key=("rate_date", "base")
        )
    assert conn.statements == []


def test_format_version_1_without_a_key_is_still_allowed():
    conn = _ScriptedConnection()
    _adapter(conn).ensure_table("s", "t", _FX, config={"format_version": 1})
    assert conn.statements[-1].endswith("WITH (format_version = 1)")


def test_merge_statement_overwrites_non_key_columns_and_inserts_the_rest():
    assert adapter_module._merge_statement(
        '"iceberg"."s"."t"', '"iceberg"."s"."st"', ["order", "Rate", "pct%"], ["order", "Rate"]
    ) == (
        'MERGE INTO "iceberg"."s"."t" AS t USING "iceberg"."s"."st" AS s '
        'ON (t."order" = s."order" AND t."Rate" = s."Rate") '
        'WHEN MATCHED THEN UPDATE SET "pct%" = s."pct%" '
        'WHEN NOT MATCHED THEN INSERT ("order", "Rate", "pct%") '
        'VALUES (s."order", s."Rate", s."pct%")'
    )


def test_merge_statement_with_every_column_in_the_key_has_no_matched_branch():
    assert adapter_module._merge_statement(
        '"iceberg"."s"."t"', '"iceberg"."s"."st"', ["a", "b"], ["a", "b"]
    ) == (
        'MERGE INTO "iceberg"."s"."t" AS t USING "iceberg"."s"."st" AS s '
        'ON (t."a" = s."a" AND t."b" = s."b") '
        'WHEN NOT MATCHED THEN INSERT ("a", "b") VALUES (s."a", s."b")'
    )


def test_upsert_stages_then_merges_once_then_drops_the_stage():
    conn = _ScriptedConnection()
    _adapter(conn).upsert("s", "t", pa.table({"id": [1, 2], "v": ["a", "b"]}), ["id"])
    s = conn.statements
    stage = _ref(s[1], "stage")
    assert s[2] == f'INSERT INTO {stage} ("id", "v") VALUES (?, ?), (?, ?)'
    assert s[3] == adapter_module._merge_statement('"iceberg"."s"."t"', stage, ["id", "v"], ["id"])
    assert s[4] == f"DROP TABLE {stage}"
    assert len(s) == 5


def test_upsert_of_zero_rows_creates_no_stage():
    conn = _ScriptedConnection()
    _adapter(conn).upsert("s", "t", pa.table({"id": pa.array([], pa.int64())}), ["id"])
    assert conn.statements == []


def test_upsert_with_a_repeated_key_runs_no_statement():
    conn = _ScriptedConnection()
    with pytest.raises(DuplicateKeyError):
        _adapter(conn).upsert("s", "t", pa.table({"id": [1, 1]}), ["id"])
    assert conn.statements == []
