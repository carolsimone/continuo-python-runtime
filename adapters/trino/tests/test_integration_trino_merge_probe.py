"""Probe (spec section 8): Trino 483 MERGE on Iceberg, as the trino adapter's _upsert runs it.

The statement shape is the one _merge_statement emits: MERGE INTO target USING a
stage table, key equality in ON, every other column in WHEN MATCHED, and
WHEN NOT MATCHED alone when every column is in the key. Pinned against the
compose stack's Iceberg REST catalog.
"""
import os
import uuid

import pytest

import trino

from continuo_trino_adapter.adapter import TrinoAdapter

pytestmark = pytest.mark.integration


def _adapter() -> TrinoAdapter:
    conn = trino.dbapi.connect(
        host="localhost",
        port=int(os.environ.get("VR_IT_TRINO_PORT", "18080")),
        user="continuo",
        catalog="iceberg",
        http_scheme="http",
    )
    return TrinoAdapter(conn, "iceberg")


@pytest.fixture
def probe():
    adapter = _adapter()
    schema = f"merge_probe_{uuid.uuid4().hex[:8]}"
    adapter._execute(f'CREATE SCHEMA "iceberg"."{schema}"')
    yield adapter, schema
    adapter._execute(f'DROP SCHEMA IF EXISTS "iceberg"."{schema}" CASCADE')
    adapter.close()


def _target_and_stage(adapter, schema, target_properties=""):
    target = f'"iceberg"."{schema}"."target"'
    stage = f'"iceberg"."{schema}"."stage"'
    adapter._execute(f'CREATE TABLE {target} ("id" BIGINT NOT NULL, "v" VARCHAR){target_properties}')
    adapter._execute(f"INSERT INTO {target} VALUES (1, 'a'), (2, 'b')")
    adapter._execute(f'CREATE TABLE {stage} ("id" BIGINT NOT NULL, "v" VARCHAR)')
    return target, stage


def _merge(target, stage):
    return (
        f'MERGE INTO {target} AS t USING {stage} AS s ON (t."id" = s."id") '
        'WHEN MATCHED THEN UPDATE SET "v" = s."v" '
        'WHEN NOT MATCHED THEN INSERT ("id", "v") VALUES (s."id", s."v")'
    )


def _rows(adapter, target):
    return [tuple(row) for row in adapter._execute(f'SELECT "id", "v" FROM {target} ORDER BY "id"')]


def test_merge_updates_and_inserts_in_one_statement(probe):
    adapter, schema = probe
    target, stage = _target_and_stage(adapter, schema)
    adapter._execute(f"INSERT INTO {stage} VALUES (2, 'B'), (3, 'c')")
    adapter._execute(_merge(target, stage))
    assert _rows(adapter, target) == [(1, "a"), (2, "B"), (3, "c")]


def test_merge_with_only_when_not_matched_inserts_only_new_keys(probe):
    adapter, schema = probe
    target, stage = _target_and_stage(adapter, schema)
    adapter._execute(f"INSERT INTO {stage} VALUES (2, 'b'), (3, 'c')")
    insert_only = (
        f'MERGE INTO {target} AS t USING {stage} AS s ON (t."id" = s."id" AND t."v" = s."v") '
        'WHEN NOT MATCHED THEN INSERT ("id", "v") VALUES (s."id", s."v")'
    )
    adapter._execute(insert_only)
    adapter._execute(insert_only)
    assert _rows(adapter, target) == [(1, "a"), (2, "b"), (3, "c")]


def test_merge_on_format_version_1_fails_and_leaves_the_target_unchanged(probe):
    adapter, schema = probe
    target, stage = _target_and_stage(adapter, schema, " WITH (format_version = 1)")
    adapter._execute(f"INSERT INTO {stage} VALUES (2, 'B')")
    with pytest.raises(trino.exceptions.TrinoQueryError, match="format version"):
        adapter._execute(_merge(target, stage))
    assert _rows(adapter, target) == [(1, "a"), (2, "b")]


def test_two_stage_rows_matching_one_target_row_fail(probe):
    adapter, schema = probe
    target, stage = _target_and_stage(adapter, schema)
    adapter._execute(f"INSERT INTO {stage} VALUES (2, 'x'), (2, 'y')")
    with pytest.raises(trino.exceptions.TrinoQueryError):
        adapter._execute(_merge(target, stage))
    assert _rows(adapter, target) == [(1, "a"), (2, "b")]
