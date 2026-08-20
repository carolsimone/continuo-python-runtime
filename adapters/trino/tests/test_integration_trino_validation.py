"""Tier-2 live tests against Trino + Iceberg REST (docker-compose.integration.yml).

Run the stack first:
    docker compose -f docker-compose.integration.yml --profile trino up -d --wait
"""
import os
import uuid

from concurrent.futures import ThreadPoolExecutor

import pytest

from continuo_python_runtime_trino.adapter import TrinoAdapter

TRINO_ENV = {
    "TRINO_HOST": "localhost",
    "TRINO_PORT": os.environ.get("VR_IT_TRINO_PORT", "18080"),
    "TRINO_USER": "continuo",
    "TRINO_CATALOG": "iceberg",
    "TRINO_HTTP_SCHEME": "http",
}


def _adapter() -> TrinoAdapter:
    """Build an adapter from the live stack's connection env."""
    for key, value in TRINO_ENV.items():
        os.environ[key] = value
    os.environ.pop("TRINO_PASSWORD", None)
    return TrinoAdapter.from_env()


def _schema_exists(adapter: TrinoAdapter, schema: str) -> bool:
    rows = adapter._execute("SHOW SCHEMAS FROM iceberg")
    return any(row[0] == schema for row in rows)


@pytest.fixture()
def candidate_schema():
    """Yield a unique candidate schema name, dropped after the test."""
    schema = f"_candidate_it_{uuid.uuid4().hex[:8]}"
    yield schema
    cleanup = _adapter()
    cleanup.drop_schema(schema)
    cleanup.close()


@pytest.mark.integration
def test_ensure_schema_creates_and_is_idempotent(candidate_schema):
    """ensure_schema creates the schema; a second call succeeds unchanged."""
    a = _adapter()
    a.ensure_schema(candidate_schema)
    assert _schema_exists(a, candidate_schema)
    a.ensure_schema(candidate_schema)  # must not raise
    assert _schema_exists(a, candidate_schema)
    a.close()


@pytest.mark.integration
def test_drop_schema_removes_schema_containing_tables(candidate_schema):
    """Trino has no DROP SCHEMA CASCADE: drop_schema must drop the tables first."""
    a = _adapter()
    a.ensure_schema(candidate_schema)
    # Two tables, created directly so this test does not depend on the build methods.
    for table in ("t_one", "t_two"):
        a._execute(
            f'CREATE TABLE iceberg."{candidate_schema}"."{table}" AS '
            f"SELECT 1 AS id WITH NO DATA"
        )
    a.drop_schema(candidate_schema)
    assert not _schema_exists(a, candidate_schema)
    a.close()


@pytest.mark.integration
def test_drop_schema_on_absent_schema_is_a_noop():
    """Teardown must never fail a release for an already-clean warehouse."""
    a = _adapter()
    a.drop_schema(f"_candidate_missing_{uuid.uuid4().hex[:8]}")  # must not raise
    a.close()


def _columns(adapter: TrinoAdapter, schema: str, table: str) -> list[tuple[str, str]]:
    rows = adapter._execute(
        "SELECT column_name, data_type FROM iceberg.information_schema.columns "
        f"WHERE table_schema = '{schema}' AND table_name = '{table}' "
        "ORDER BY ordinal_position"
    )
    return [(row[0], row[1]) for row in rows]


def _count(adapter: TrinoAdapter, schema: str, table: str) -> int:
    rows = adapter._execute(f'SELECT count(*) FROM iceberg."{schema}"."{table}"')
    return int(rows[0][0])


@pytest.fixture()
def prod_table():
    """Yield a prod-like schema with one populated table; dropped after the test."""
    schema = f"_prod_it_{uuid.uuid4().hex[:8]}"
    a = _adapter()
    a.ensure_schema(schema)
    a._execute(
        f'CREATE TABLE iceberg."{schema}"."src_table" AS '
        "SELECT * FROM (VALUES (1, 'a'), (2, 'b')) AS t(id, name)"
    )
    a.close()
    yield schema
    cleanup = _adapter()
    cleanup.drop_schema(schema)
    cleanup.close()


@pytest.mark.integration
def test_build_empty_from_sql_live(candidate_schema, prod_table):
    """Build an empty candidate table shaped by a compiled SELECT."""
    a = _adapter()
    a.ensure_schema(candidate_schema)
    a.build_empty_from_sql(
        candidate_schema, "built",
        f'SELECT id, name FROM iceberg."{prod_table}"."src_table"',
    )
    assert _columns(a, candidate_schema, "built") == [("id", "integer"), ("name", "varchar")]
    assert _count(a, candidate_schema, "built") == 0
    a.close()


@pytest.mark.integration
def test_build_is_rerun_idempotent(candidate_schema, prod_table):
    """Building the same table twice drops and recreates instead of failing."""
    a = _adapter()
    a.ensure_schema(candidate_schema)
    for _ in range(2):
        a.build_empty_from_sql(
            candidate_schema, "rerun",
            f'SELECT id FROM iceberg."{prod_table}"."src_table"',
        )
    assert _count(a, candidate_schema, "rerun") == 0
    a.close()


@pytest.mark.integration
def test_clone_empty_from_prod_live(candidate_schema, prod_table):
    """Clone an empty candidate table shaped like the prod table."""
    a = _adapter()
    a.ensure_schema(candidate_schema)
    a.clone_empty_from_prod(candidate_schema, prod_table, "src_table")
    assert _columns(a, candidate_schema, "src_table") == [
        ("id", "integer"), ("name", "varchar"),
    ]
    assert _count(a, candidate_schema, "src_table") == 0
    a.close()


@pytest.mark.integration
def test_drop_schema_removes_schema_containing_views(candidate_schema):
    """Trino's SHOW TABLES lists views too; drop_schema must remove them as well."""
    a = _adapter()
    a.ensure_schema(candidate_schema)
    a._execute(
        f'CREATE TABLE iceberg."{candidate_schema}"."base_t" AS SELECT 1 AS id WITH NO DATA'
    )
    a._execute(
        f'CREATE VIEW iceberg."{candidate_schema}"."v_one" AS '
        f'SELECT id FROM iceberg."{candidate_schema}"."base_t"'
    )
    a.drop_schema(candidate_schema)
    assert not _schema_exists(a, candidate_schema)
    a.close()


def _nullability(adapter: TrinoAdapter, schema: str, table: str) -> list[tuple[str, str]]:
    rows = adapter._execute(
        "SELECT column_name, is_nullable FROM iceberg.information_schema.columns "
        f"WHERE table_schema = '{schema}' AND table_name = '{table}' "
        "ORDER BY ordinal_position"
    )
    return [(row[0], row[1]) for row in rows]


@pytest.mark.integration
def test_build_empty_from_columns_creates_typed_empty_table(candidate_schema):
    """Typed CREATE TABLE carries name, type and NOT NULL into the DDL.

    Trino/Iceberg does accept and enforce NOT NULL, so ``nullable: false`` must
    reach the DDL here exactly as it does on postgres. What Iceberg does *not*
    carry is VARCHAR length — see the data_type assertion below.
    """
    a = _adapter()
    a.ensure_schema(candidate_schema)
    a.build_empty_from_columns(candidate_schema, "t", [
        {"name": "id", "type": "INTEGER", "nullable": False},
        {"name": "email", "type": "VARCHAR(255)"},
        {"name": "amount", "type": "NUMERIC(10,2)"},
    ], {})
    # Trino's information_schema.columns.data_type reports "varchar" without
    # its length (unlike "decimal(10,2)", which does carry precision/scale).
    assert _columns(a, candidate_schema, "t") == [
        ("id", "integer"), ("email", "varchar"), ("amount", "decimal(10,2)"),
    ]
    assert _nullability(a, candidate_schema, "t") == [
        ("id", "NO"),        # nullable: false -> NOT NULL
        ("email", "YES"),    # nullable absent -> defaults to nullable
        ("amount", "YES"),
    ]
    assert _count(a, candidate_schema, "t") == 0
    a.close()


@pytest.mark.integration
def test_build_empty_from_columns_not_null_is_enforced_on_insert(candidate_schema):
    """The declared NOT NULL is a real constraint, not just catalog metadata."""
    a = _adapter()
    a.ensure_schema(candidate_schema)
    a.build_empty_from_columns(candidate_schema, "t_nn", [
        {"name": "id", "type": "INTEGER", "nullable": False},
        {"name": "email", "type": "VARCHAR(255)"},
    ], {})
    ref = f'iceberg."{candidate_schema}".t_nn'
    with pytest.raises(Exception):  # CONSTRAINT_VIOLATION on the NOT NULL column
        a._execute(f"INSERT INTO {ref} VALUES (NULL, 'x')")
    a._execute(f"INSERT INTO {ref} VALUES (1, NULL)")  # nullable column: accepted
    assert _count(a, candidate_schema, "t_nn") == 1
    a.close()


@pytest.mark.integration
def test_build_empty_from_columns_rebuilds_on_rerun(candidate_schema):
    """A second call drops and recreates the table rather than merging columns."""
    a = _adapter()
    a.ensure_schema(candidate_schema)
    a.build_empty_from_columns(candidate_schema, "t2", [{"name": "id", "type": "INTEGER"}], {})
    a.build_empty_from_columns(candidate_schema, "t2", [{"name": "renamed", "type": "BIGINT"}], {})
    assert [c[0] for c in _columns(a, candidate_schema, "t2")] == ["renamed"]  # drop-then-create
    a.close()


@pytest.mark.integration
def test_check_binds_passes_on_valid_read_against_empty_upstream(candidate_schema):
    """check_binds does not raise for a query that binds against known columns."""
    a = _adapter()
    a.ensure_schema(candidate_schema)
    a.build_empty_from_columns(candidate_schema, "up", [
        {"name": "a", "type": "INTEGER"}, {"name": "b", "type": "TEXT"},
    ], {})
    a.check_binds(f'select a, b from iceberg."{candidate_schema}".up')  # must not raise
    a.close()


@pytest.mark.integration
def test_check_binds_passes_on_read_ending_in_line_comment(candidate_schema):
    """A read ending in a trailing `--` comment must not swallow the wrap's close-paren."""
    a = _adapter()
    a.ensure_schema(candidate_schema)
    a.build_empty_from_columns(candidate_schema, "up5", [
        {"name": "a", "type": "INTEGER"}, {"name": "b", "type": "TEXT"},
    ], {})
    # must not raise
    a.check_binds(f'select a, b from iceberg."{candidate_schema}".up5 -- trailing comment')
    a.close()


@pytest.mark.integration
def test_check_binds_raises_on_missing_column(candidate_schema):
    """check_binds raises when a selected column does not exist upstream."""
    a = _adapter()
    a.ensure_schema(candidate_schema)
    a.build_empty_from_columns(candidate_schema, "up2", [{"name": "a", "type": "INTEGER"}], {})
    with pytest.raises(Exception):
        a.check_binds(f'select a, dropped_col from iceberg."{candidate_schema}".up2')
    a.close()


@pytest.mark.integration
def test_check_binds_raises_on_missing_table(candidate_schema):
    """check_binds raises when the referenced table does not exist."""
    a = _adapter()
    with pytest.raises(Exception):
        a.check_binds(f'select 1 from iceberg."{candidate_schema}".does_not_exist')
    a.close()


@pytest.mark.integration
def test_check_binds_rejects_stacked_statement_and_executes_nothing(candidate_schema):
    """A stacked statement is rejected at parse time; the appended DML never runs."""
    a = _adapter()
    a.ensure_schema(candidate_schema)
    a.build_empty_from_columns(candidate_schema, "up3", [{"name": "a", "type": "INTEGER"}], {})
    a._execute(f'INSERT INTO iceberg."{candidate_schema}".up3 VALUES (1), (2), (3)')
    with pytest.raises(Exception):
        a.check_binds(
            f'select a from iceberg."{candidate_schema}".up3; '
            f'delete from iceberg."{candidate_schema}".up3'
        )
    assert _count(a, candidate_schema, "up3") == 3  # the appended DELETE never executed
    a.close()


@pytest.mark.integration
def test_check_binds_rejects_bare_dml(candidate_schema):
    """Bare DML fails the bind check instead of silently EXPLAIN-passing."""
    a = _adapter()
    a.ensure_schema(candidate_schema)
    a.build_empty_from_columns(candidate_schema, "up4", [{"name": "a", "type": "INTEGER"}], {})
    a._execute(f'INSERT INTO iceberg."{candidate_schema}".up4 VALUES (1), (2)')
    with pytest.raises(Exception):
        a.check_binds(f'delete from iceberg."{candidate_schema}".up4')
    assert _count(a, candidate_schema, "up4") == 2  # the DELETE never executed
    a.close()


@pytest.mark.integration
def test_check_binds_rejects_paren_balanced_escape_and_executes_nothing(candidate_schema):
    """A read that balances the wrap's own parens must not smuggle a DELETE past it.

    Trino's protocol refuses multi-statement input on its own, so this is a
    belt-and-braces check that the contract's parse gate agrees with it. The
    row count is the assertion that matters — a rejection raised *after* the
    DELETE ran would still satisfy pytest.raises.
    """
    a = _adapter()
    a.ensure_schema(candidate_schema)
    a.build_empty_from_columns(candidate_schema, "up6", [{"name": "a", "type": "INTEGER"}], {})
    a._execute(f'INSERT INTO iceberg."{candidate_schema}".up6 VALUES (1), (2), (3)')
    with pytest.raises(Exception):
        a.check_binds(
            f'select 1) AS x; DELETE FROM iceberg."{candidate_schema}".up6; '
            "SELECT * FROM (SELECT 1"
        )
    assert _count(a, candidate_schema, "up6") == 3  # the injected DELETE never executed
    a.close()


@pytest.mark.integration
def test_check_binds_rejects_paren_balanced_escape_carrying_ddl(candidate_schema):
    """The same escape carrying a DROP TABLE must leave the table standing."""
    a = _adapter()
    a.ensure_schema(candidate_schema)
    a.build_empty_from_columns(candidate_schema, "up7", [{"name": "a", "type": "INTEGER"}], {})
    a._execute(f'INSERT INTO iceberg."{candidate_schema}".up7 VALUES (1), (2), (3)')
    with pytest.raises(Exception):
        a.check_binds(
            f'select 1) AS x; DROP TABLE iceberg."{candidate_schema}".up7; '
            "SELECT * FROM (SELECT 1"
        )
    assert _count(a, candidate_schema, "up7") == 3  # the injected DROP never executed
    a.close()


@pytest.mark.integration
def test_ensure_schema_concurrent_callers_all_succeed():
    """Parallel root validation nodes race CREATE SCHEMA; every caller must succeed.

    The Iceberg REST metastore can report the loser of the create race as a
    query-level error rather than a user error; ensure_schema must treat any
    outcome where the schema ends up existing as success.
    """
    for _ in range(4):
        schema = f"_candidate_race_{uuid.uuid4().hex[:8]}"
        adapters = [_adapter() for _ in range(8)]
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(a.ensure_schema, schema) for a in adapters]
                for future in futures:
                    future.result()  # raises if any caller failed
            assert _schema_exists(adapters[0], schema)
        finally:
            for a in adapters:
                a.close()
            cleanup = _adapter()
            cleanup.drop_schema(schema)
            cleanup.close()


def _show_create(adapter: TrinoAdapter, schema: str, table: str) -> str:
    """Return SHOW CREATE TABLE's text for iceberg.<schema>.<table>."""
    rows = adapter._execute(f'SHOW CREATE TABLE iceberg."{schema}"."{table}"')
    return rows[0][0]


def _table_exists(adapter: TrinoAdapter, schema: str, table: str) -> bool:
    rows = adapter._execute(f'SHOW TABLES FROM iceberg."{schema}"')
    return any(row[0] == table for row in rows)


@pytest.mark.integration
def test_config_properties_are_applied_to_the_built_table(candidate_schema):
    """A valid config lands as real Iceberg table properties on the created table."""
    a = _adapter()
    a.ensure_schema(candidate_schema)
    a.build_empty_from_columns(
        candidate_schema, "laid_out",
        [
            {"name": "id", "type": "BIGINT", "nullable": False},
            {"name": "ts", "type": "TIMESTAMP"},
        ],
        # ORC and format_version 1 are both NON-default (a bare Iceberg table
        # reports PARQUET / 2), so these assertions prove the config was applied
        # rather than re-reading the connector's own defaults back.
        {
            "partitioning": ["day(ts)"], "sorted_by": ["id"],
            "format": "ORC", "format_version": 1,
        },
    )
    ddl = _show_create(a, candidate_schema, "laid_out")
    a.close()
    assert "partitioning = ARRAY['day(ts)']" in ddl
    # Iceberg normalizes a bare sort column to its full ordering spelling.
    assert "sorted_by = ARRAY['id ASC NULLS FIRST']" in ddl
    assert "format = 'ORC'" in ddl
    assert "format_version = 1" in ddl


@pytest.mark.integration
def test_unknown_config_key_rejects_and_leaves_no_table(candidate_schema):
    """Fail closed: an unknown key rejects before the DROP, so nothing is half-built."""
    a = _adapter()
    a.ensure_schema(candidate_schema)
    with pytest.raises(ValueError, match="sortkey"):
        a.build_empty_from_columns(
            candidate_schema, "never_built", [{"name": "id", "type": "BIGINT"}],
            {"sortkey": ["id"]},
        )
    assert not _table_exists(a, candidate_schema, "never_built")
    a.close()


@pytest.mark.integration
def test_partitioning_on_nonexistent_column_rejects(candidate_schema):
    """Trino itself refuses a partition column the node does not declare."""
    a = _adapter()
    a.ensure_schema(candidate_schema)
    with pytest.raises(Exception, match="nope"):
        a.build_empty_from_columns(
            candidate_schema, "bad_partition", [{"name": "id", "type": "BIGINT"}],
            {"partitioning": ["nope"]},
        )
    assert not _table_exists(a, candidate_schema, "bad_partition")
    a.close()


@pytest.mark.integration
def test_empty_config_adds_no_layout_properties(candidate_schema):
    """Back-compat pin: no config means the bare 0.4.0 CREATE TABLE and nothing else."""
    a = _adapter()
    a.ensure_schema(candidate_schema)
    a.build_empty_from_columns(
        candidate_schema, "bare", [{"name": "id", "type": "BIGINT", "nullable": False}], {}
    )
    ddl = _show_create(a, candidate_schema, "bare")
    a.close()
    # Iceberg always reports its own format/format_version/location defaults, so
    # the pin is that no LAYOUT property appears — the adapter added no clause.
    assert "partitioning" not in ddl
    assert "sorted_by" not in ddl
    assert "id bigint NOT NULL" in ddl


@pytest.mark.integration
def test_config_properties_survive_a_rebuild(candidate_schema):
    """Drop-then-create must re-apply the layout, not silently lose it on rerun."""
    a = _adapter()
    a.ensure_schema(candidate_schema)
    columns = [{"name": "id", "type": "BIGINT"}, {"name": "ts", "type": "TIMESTAMP"}]
    config = {"partitioning": ["day(ts)"]}
    a.build_empty_from_columns(candidate_schema, "rebuilt", columns, config)
    a.build_empty_from_columns(candidate_schema, "rebuilt", columns, config)
    ddl = _show_create(a, candidate_schema, "rebuilt")
    a.close()
    assert "partitioning = ARRAY['day(ts)']" in ddl
