"""Validation behaviours against a real DuckLake (postgres catalog + minio data)."""
import concurrent.futures

import duckdb
import pyarrow as pa
import pytest

pytestmark = pytest.mark.integration


def _schema_exists(adapter, schema: str) -> bool:
    rows = adapter.fetch(
        "SELECT count(*) AS n FROM information_schema.schemata "
        f"WHERE catalog_name = 'lake' AND schema_name = '{schema}'"
    ).to_pylist()
    return rows[0]["n"] == 1


def test_ensure_schema_creates_and_is_idempotent(adapter, schema):
    assert not _schema_exists(adapter, schema)
    adapter.ensure_schema(schema)
    adapter.ensure_schema(schema)
    assert _schema_exists(adapter, schema)


def test_ensure_schema_race_all_callers_succeed(adapter_factory, adapter, schema):
    assert not _schema_exists(adapter, schema)

    def call(_):
        adapter_factory().ensure_schema(schema)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(call, range(8)))  # any failure propagates here
    assert _schema_exists(adapter, schema)


def test_drop_schema_removes_a_schema_containing_tables(adapter, schema, tables_in):
    adapter.ensure_table(schema, "a", [{"name": "id", "type": "INTEGER", "nullable": True}], config={})
    adapter.drop_schema(schema)
    assert not _schema_exists(adapter, schema)
    assert tables_in(schema) == []


def test_drop_schema_on_an_absent_schema_is_a_noop(adapter, schema):
    adapter.drop_schema(schema)
    adapter.drop_schema(schema)


def test_build_empty_from_sql_creates_an_empty_table_with_the_reads_shape(
    adapter, schema, prod_table, columns_of, scalar
):
    prod, src = prod_table
    adapter.ensure_schema(schema)
    adapter.build_empty_from_sql(schema, "m", f'SELECT id, name FROM "{prod}"."{src}";')
    assert columns_of(schema, "m") == [("id", "INTEGER", "YES"), ("name", "VARCHAR", "YES")]
    assert scalar(f'SELECT count(*) AS n FROM "{schema}"."m"') == 0


def test_build_is_rerun_idempotent(adapter, schema, prod_table, columns_of):
    prod, src = prod_table
    adapter.ensure_schema(schema)
    sql = f'SELECT id FROM "{prod}"."{src}"'
    adapter.build_empty_from_sql(schema, "m", sql)
    adapter.build_empty_from_sql(schema, "m", sql)
    assert columns_of(schema, "m") == [("id", "INTEGER", "YES")]


def test_build_empty_from_sql_keeps_a_read_ending_in_a_line_comment(adapter, schema, prod_table, columns_of):
    prod, src = prod_table
    adapter.ensure_schema(schema)
    adapter.build_empty_from_sql(schema, "m", f'SELECT id FROM "{prod}"."{src}" -- trailing')
    assert columns_of(schema, "m") == [("id", "INTEGER", "YES")]


def test_clone_empty_from_prod_copies_the_shape_not_the_rows(adapter, schema, prod_table, columns_of, scalar):
    prod, src = prod_table
    adapter.ensure_schema(schema)
    adapter.clone_empty_from_prod(schema, prod, src)
    assert columns_of(schema, src) == [("id", "INTEGER", "YES"), ("name", "VARCHAR", "YES")]
    assert scalar(f'SELECT count(*) AS n FROM "{schema}"."{src}"') == 0


def test_build_empty_from_columns_creates_a_typed_empty_table(adapter, schema, columns_of, scalar):
    adapter.ensure_schema(schema)
    adapter.build_empty_from_columns(
        schema, "typed",
        [{"name": "id", "type": "BIGINT", "nullable": False},
         {"name": "label", "type": "VARCHAR(10)", "nullable": True},
         {"name": "amount", "type": "NUMERIC(10,2)", "nullable": False}],
        {},
    )
    assert columns_of(schema, "typed") == [
        ("id", "BIGINT", "NO"), ("label", "VARCHAR", "YES"), ("amount", "DECIMAL(10,2)", "NO"),
    ]
    assert scalar(f'SELECT count(*) AS n FROM "{schema}"."typed"') == 0


def test_not_null_is_enforced_on_load(adapter, schema, scalar):
    adapter.ensure_schema(schema)
    adapter.build_empty_from_columns(schema, "t", [{"name": "id", "type": "INTEGER", "nullable": False}], {})
    with pytest.raises(duckdb.Error, match="(?i)not null"):
        adapter.load(schema, "t", pa.table({"id": pa.array([None], pa.int32())}))
    assert scalar(f'SELECT count(*) AS n FROM "{schema}"."t"') == 0


def test_build_empty_from_columns_rebuilds_on_rerun(adapter, schema, columns_of):
    adapter.ensure_schema(schema)
    adapter.build_empty_from_columns(schema, "t", [{"name": "a", "type": "INTEGER"}], {})
    adapter.build_empty_from_columns(schema, "t", [{"name": "b", "type": "TEXT"}, {"name": "c", "type": "DATE"}], {})
    assert columns_of(schema, "t") == [("b", "VARCHAR", "YES"), ("c", "DATE", "YES")]


def test_an_engine_rejected_rebuild_rolls_back_and_keeps_the_prior_table(adapter, schema, columns_of, scalar):
    adapter.ensure_schema(schema)
    adapter.build_empty_from_columns(schema, "t", [{"name": "keep", "type": "INTEGER"}], {})
    adapter.load(schema, "t", pa.table({"keep": pa.array([1], pa.int32())}))
    with pytest.raises(duckdb.Error):  # duplicate column names: rejected by the engine after the DROP
        adapter.build_empty_from_columns(
            schema, "t", [{"name": "dup", "type": "INTEGER"}, {"name": "dup", "type": "INTEGER"}], {}
        )
    assert columns_of(schema, "t") == [("keep", "INTEGER", "YES")]
    assert scalar(f'SELECT count(*) AS n FROM "{schema}"."t"') == 1
    adapter.fetch("SELECT 1 AS still_usable")  # the connection is not left in an aborted transaction


@pytest.mark.parametrize("read", [
    "SELECT id, name FROM {prod}.src_table",
    "SELECT id FROM {prod}.src_table -- trailing comment",
    "SELECT ';' AS semi",
    "WITH c AS (SELECT id FROM {prod}.src_table) SELECT id FROM c",
    "VALUES (1), (2)",
])
def test_check_binds_passes_a_valid_single_read(adapter, prod_table, read):
    prod, _ = prod_table
    adapter.check_binds(read.format(prod=f'"{prod}"'))


def test_check_binds_raises_on_a_missing_column(adapter, prod_table):
    prod, _ = prod_table
    with pytest.raises(duckdb.Error):
        adapter.check_binds(f'SELECT nope FROM "{prod}".src_table')


def test_check_binds_raises_on_a_missing_table(adapter, prod_table):
    prod, _ = prod_table
    with pytest.raises(duckdb.Error):
        adapter.check_binds(f'SELECT id FROM "{prod}".does_not_exist')


def test_check_binds_scans_no_data(adapter, prod_table, scalar):
    prod, src = prod_table
    # error() fires only if a row is evaluated (id is 1 and 2, so the branch is taken at
    # runtime); EXPLAIN binds the expression without running it.
    adapter.check_binds(f'SELECT CASE WHEN id > 0 THEN error(\'scanned\') ELSE 0 END AS v FROM "{prod}"."{src}"')
    assert scalar(f'SELECT count(*) AS n FROM "{prod}"."{src}"') == 2  # untouched


@pytest.mark.parametrize("attack", [
    'SELECT 1; DROP TABLE "{s}"."victim"',
    'SELECT \';\'; DROP TABLE "{s}"."victim"',
    'SELECT 1) AS x; DROP TABLE "{s}"."victim"; SELECT * FROM (SELECT 1',
    'DELETE FROM "{s}"."victim"',
    'DROP TABLE "{s}"."victim"',
])
def test_check_binds_rejects_stacked_and_non_read_statements_and_executes_nothing(
    adapter, schema, tables_in, scalar, attack
):
    adapter.ensure_table(schema, "victim", [{"name": "id", "type": "INTEGER", "nullable": True}], config={})
    adapter.load(schema, "victim", pa.table({"id": pa.array([1, 2, 3], pa.int32())}))
    with pytest.raises(ValueError):
        adapter.check_binds(attack.format(s=schema))
    assert tables_in(schema) == ["victim"]
    assert scalar(f'SELECT count(*) AS n FROM "{schema}"."victim"') == 3  # no row deleted


def test_check_binds_leaves_the_connection_usable_after_a_failed_read(adapter, prod_table):
    prod, _ = prod_table
    with pytest.raises(duckdb.Error):
        adapter.check_binds(f'SELECT nope FROM "{prod}".src_table')
    assert adapter.fetch("SELECT 1 AS ok").to_pylist() == [{"ok": 1}]
    adapter.check_binds(f'SELECT id FROM "{prod}".src_table')
