"""Tier-2 live tests against Trino + Iceberg REST (docker-compose.integration.yml).

Run the stack first:
    docker compose -f docker-compose.integration.yml --profile trino up -d --wait

Note on entry-point discovery: this suite does NOT call
``discover_runtime_adapter()`` and assert it returns trino. In a dev venv where
both runtime packages are installed (as they are in this workspace), the
``continuo_runtime.adapters`` group has two entries and global discovery
deliberately raises. The in-repo test therefore selects the named ``trino`` entry
point; runner images separately enforce the exactly-one-plugin invariant.
"""
import decimal
import os
import uuid

from importlib.metadata import entry_points

import pyarrow as pa
import pytest

import trino

from continuo_python_runtime_trino.adapter import TrinoRuntimeAdapter

TRINO_ENV = {
    "TRINO_HOST": "localhost",
    "TRINO_PORT": os.environ.get("VR_IT_TRINO_PORT", "18080"),
    "TRINO_USER": "continuo",
    "TRINO_CATALOG": "iceberg",
    "TRINO_HTTP_SCHEME": "http",
}


def _adapter() -> TrinoRuntimeAdapter:
    """Build an adapter from the live stack's connection env."""
    for key, value in TRINO_ENV.items():
        os.environ[key] = value
    os.environ.pop("TRINO_PASSWORD", None)
    return TrinoRuntimeAdapter.from_env()


@pytest.fixture()
def clean_schema():
    """Drop a fresh, uniquely-named schema before and after each test."""
    schema = f"runtime-it-{uuid.uuid4().hex[:8]}"
    a = _adapter()
    a._execute(f'DROP SCHEMA IF EXISTS iceberg."{schema}" CASCADE')
    a.close()
    yield schema
    a = _adapter()
    a._execute(f'DROP SCHEMA IF EXISTS iceberg."{schema}" CASCADE')
    a.close()


def _columns(schema: str, table: str) -> list[tuple]:
    a = _adapter()
    rows = a._execute(
        "SELECT column_name, data_type, is_nullable FROM iceberg.information_schema.columns "
        f"WHERE table_schema = '{schema}' AND table_name = '{table}' "
        "ORDER BY ordinal_position"
    )
    a.close()
    return [tuple(row) for row in rows]


def _count(schema: str, table: str) -> int:
    a = _adapter()
    rows = a._execute(f'SELECT count(*) FROM iceberg."{schema}"."{table}"')
    a.close()
    return int(rows[0][0])


def _seed(schema: str, table: str, ddl: str, rows_sql: str) -> None:
    a = _adapter()
    a._execute(f'CREATE SCHEMA IF NOT EXISTS iceberg."{schema}"')
    a._execute(f'DROP TABLE IF EXISTS iceberg."{schema}"."{table}"')
    a._execute(f'CREATE TABLE iceberg."{schema}"."{table}" ({ddl})')
    if rows_sql:
        a._execute(rows_sql)
    a.close()


@pytest.mark.integration
def test_ensure_table_creates_typed_table_with_not_null(clean_schema):
    """ensure_table creates a typed table; NOT NULL columns show up in the DDL.

    Verified live against Trino 483 + the Iceberg REST catalog: unlike some
    connector/version combinations, NOT NULL column constraints ARE supported
    here (see the module/adapter docstring for the probe that established this).
    """
    a = _adapter()
    a.ensure_table(
        clean_schema,
        "typed",
        [
            {"name": "id", "type": "BIGINT", "nullable": False},
            {"name": "label", "type": "VARCHAR(10)", "nullable": True},
            {"name": "amount", "type": "NUMERIC(10,2)", "nullable": False},
        ],
    )
    a.close()
    cols = _columns(clean_schema, "typed")
    assert cols == [
        ("id", "bigint", "NO"),
        ("label", "varchar", "YES"),
        ("amount", "decimal(10,2)", "NO"),
    ]


@pytest.mark.integration
def test_ensure_table_is_idempotent(clean_schema):
    """Calling ensure_table twice for the same table does not fail."""
    a = _adapter()
    cols = [{"name": "id", "type": "INT", "nullable": True}]
    a.ensure_table(clean_schema, "again", cols)
    a.ensure_table(clean_schema, "again", cols)
    a.close()
    assert _columns(clean_schema, "again") == [("id", "integer", "YES")]


@pytest.mark.integration
def test_ensure_and_load_support_quoted_identifiers(clean_schema):
    """Delimited schema, table, and column names round-trip through the adapter."""
    a = _adapter()
    a.ensure_table(
        clean_schema,
        "order table",
        [{"name": 'order"id', "type": "BIGINT", "nullable": False}],
    )
    a.load(
        clean_schema,
        "order table",
        pa.table({'order"id': pa.array([7], type=pa.int64())}),
    )
    rows = a._execute(
        f'SELECT "order""id" FROM iceberg."{clean_schema}"."order table"'
    )
    a.close()
    assert rows == [[7]]


@pytest.mark.integration
def test_fetch_round_trips_rows_into_arrow(clean_schema):
    """fetch() maps trino row values to the expected Arrow types."""
    _seed(
        clean_schema,
        "src",
        "id INTEGER, name VARCHAR, amount DECIMAL(10,2), d DATE, ok BOOLEAN",
        f'INSERT INTO iceberg."{clean_schema}"."src" VALUES '
        f"(1, 'a', 12.50, DATE '2024-01-15', true), "
        f"(2, 'b', 3.00, DATE '2024-02-20', false)",
    )
    a = _adapter()
    table = a.fetch(
        f'SELECT id, name, amount, d, ok FROM iceberg."{clean_schema}"."src" ORDER BY id'
    )
    a.close()

    assert table.num_rows == 2
    assert table.column("id").to_pylist() == [1, 2]
    assert table.column("name").to_pylist() == ["a", "b"]
    assert table.column("amount").to_pylist() == [
        decimal.Decimal("12.50"), decimal.Decimal("3.00"),
    ]
    assert str(table.column("amount").type).startswith("decimal128")
    assert str(table.column("d").type) == "date32[day]"
    assert table.column("ok").to_pylist() == [True, False]


@pytest.mark.integration
def test_load_replaces_contents_atomically(clean_schema):
    """load() swaps in exactly the new rows, replacing prior junk contents."""
    _seed(
        clean_schema,
        "tgt",
        "id INTEGER, name VARCHAR",
        f'INSERT INTO iceberg."{clean_schema}"."tgt" VALUES '
        f"(9, 'junk1'), (10, 'junk2'), (11, 'junk3')",
    )
    a = _adapter()
    data = pa.table({"id": pa.array([1, 2], type=pa.int32()), "name": pa.array(["x", "y"])})
    a.load(clean_schema, "tgt", data)
    a.close()

    assert _count(clean_schema, "tgt") == 2
    check = _adapter()
    rows = check._execute(f'SELECT id, name FROM iceberg."{clean_schema}"."tgt" ORDER BY id')
    check.close()
    assert [tuple(r) for r in rows] == [(1, "x"), (2, "y")]


@pytest.mark.integration
def test_load_preserves_unrelated_suffix_tables(clean_schema):
    """Loading one target must not delete legitimate fixed-suffix table names."""
    _seed(
        clean_schema,
        "orders",
        "id INTEGER",
        f'INSERT INTO iceberg."{clean_schema}"."orders" VALUES 99',
    )
    _seed(
        clean_schema,
        "orders__stage",
        "id INTEGER",
        f'INSERT INTO iceberg."{clean_schema}"."orders__stage" VALUES 777',
    )
    _seed(
        clean_schema,
        "orders__old",
        "id INTEGER",
        f'INSERT INTO iceberg."{clean_schema}"."orders__old" VALUES 888',
    )

    a = _adapter()
    a.load(clean_schema, "orders", pa.table({"id": pa.array([1, 2], type=pa.int32())}))
    target_rows = a._execute(
        f'SELECT id FROM iceberg."{clean_schema}"."orders" ORDER BY id'
    )
    stage_rows = a._execute(
        f'SELECT id FROM iceberg."{clean_schema}"."orders__stage"'
    )
    old_rows = a._execute(f'SELECT id FROM iceberg."{clean_schema}"."orders__old"')
    tables = [row[0] for row in a._execute(f'SHOW TABLES FROM iceberg."{clean_schema}"')]
    a.close()

    assert target_rows == [[1], [2]]
    assert stage_rows == [[777]]
    assert old_rows == [[888]]
    assert not any(name.startswith("__continuo_") for name in tables)


@pytest.mark.integration
def test_load_preserves_iceberg_table_properties(clean_schema):
    """A content replacement retains connector properties but gets a fresh location."""
    a = _adapter()
    a._ensure_schema(clean_schema)
    target_ref = f'iceberg."{clean_schema}"."partitioned"'
    properties_ref = f'iceberg."{clean_schema}"."partitioned$properties"'
    a._execute(
        f"CREATE TABLE {target_ref} (id INTEGER, name VARCHAR) "
        "WITH (format = 'ORC', format_version = 1, partitioning = ARRAY['id'])"
    )
    location_before = a._execute(
        f"SELECT value FROM {properties_ref} WHERE key = 'location'"
    )[0][0]

    a.load(
        clean_schema,
        "partitioned",
        pa.table({"id": pa.array([1], type=pa.int32()), "name": pa.array(["a"])}),
    )

    create_sql = a._execute(f"SHOW CREATE TABLE {target_ref}")[0][0]
    location_after = a._execute(
        f"SELECT value FROM {properties_ref} WHERE key = 'location'"
    )[0][0]
    a.close()

    assert "format = 'ORC'" in create_sql
    assert "format_version = 1" in create_sql
    assert "partitioning = ARRAY['id']" in create_sql
    assert location_after != location_before


@pytest.mark.integration
def test_load_preserves_not_null_constraint_across_swap(clean_schema):
    """load() must not silently drop the target's NOT NULL constraint on swap.

    This is the reason CREATE OR REPLACE TABLE ... AS SELECT was rejected in
    favor of the staged rename-swap: verified live that CREATE OR REPLACE leaves
    the replaced column nullable. Here we assert the adapter's actual swap
    preserves it.
    """
    a = _adapter()
    a.ensure_table(
        clean_schema, "notnull_t",
        [{"name": "id", "type": "BIGINT", "nullable": False},
         {"name": "name", "type": "TEXT", "nullable": True}],
    )
    data = pa.table({"id": pa.array([1], type=pa.int64()), "name": pa.array(["a"])})
    a.load(clean_schema, "notnull_t", data)
    a.close()

    assert _columns(clean_schema, "notnull_t") == [
        ("id", "bigint", "NO"), ("name", "varchar", "YES"),
    ]
    check = _adapter()
    with pytest.raises(trino.exceptions.TrinoUserError):
        check._execute(f'INSERT INTO iceberg."{clean_schema}"."notnull_t" VALUES (NULL, \'z\')')
    check.close()


@pytest.mark.integration
def test_load_failure_leaves_prior_target_intact_and_cleans_up_staging(clean_schema):
    """A failing insert (NOT NULL violation) leaves the prior target untouched.

    Forces the failure via a NULL value for a NOT-NULL target column — the
    staging insert raises before the rename-swap ever touches the target — and
    asserts the staging table left behind is cleaned up (best-effort, in load()'s
    finally).
    """
    a = _adapter()
    a.ensure_table(
        clean_schema, "tgt2",
        [{"name": "id", "type": "BIGINT", "nullable": False},
         {"name": "label", "type": "VARCHAR(10)", "nullable": True}],
    )
    a.load(clean_schema, "tgt2", pa.table({
        "id": pa.array([1], type=pa.int64()), "label": pa.array(["a"]),
    }))

    # A None in an Arrow int64 array becomes a SQL NULL bound into the NOT NULL
    # id column, forcing CONSTRAINT_VIOLATION mid-staging-insert.
    bad_data = pa.table({
        "id": pa.array([2, None], type=pa.int64()), "label": pa.array(["b", "c"]),
    })
    with pytest.raises(Exception):
        a.load(clean_schema, "tgt2", bad_data)
    a.close()

    assert _count(clean_schema, "tgt2") == 1
    check = _adapter()
    rows = check._execute(f'SELECT id, label FROM iceberg."{clean_schema}"."tgt2"')
    tables = [row[0] for row in check._execute(f'SHOW TABLES FROM iceberg."{clean_schema}"')]
    check.close()
    assert [tuple(r) for r in rows] == [(1, "a")]
    assert not any(name.startswith("__continuo_stage_") for name in tables)


@pytest.mark.integration
def test_load_zero_rows_swaps_in_an_empty_table(clean_schema):
    """load() with a 0-row Arrow table swaps in an empty target."""
    a = _adapter()
    a.ensure_table(clean_schema, "tgt3", [{"name": "id", "type": "INT", "nullable": True}])
    a.load(clean_schema, "tgt3", pa.table({
        "id": pa.array([1, 2], type=pa.int32()),
    }))
    a.load(clean_schema, "tgt3", pa.table({"id": pa.array([], type=pa.int32())}))
    a.close()

    assert _count(clean_schema, "tgt3") == 0


@pytest.mark.integration
def test_fetch_rejects_duplicate_select_columns(clean_schema):
    """fetch() raises rather than silently keeping only the last of two same-named columns."""
    a = _adapter()
    with pytest.raises(ValueError, match="id"):
        a.fetch("SELECT 1 AS id, 2 AS id")
    a.close()


def test_entry_point_resolves_to_this_adapter():
    """The `trino` runtime entry point is registered and loads TrinoRuntimeAdapter.

    Does NOT call discover_runtime_adapter(): see the module docstring — with
    continuo-python-runtime-postgres also installed in this dev venv, two entry points
    are registered under continuo_runtime.adapters, and discover_runtime_adapter()
    deliberately raises when more than one is installed. Only a runner image
    (which installs exactly one engine package) can rely on discovery choosing
    trino; here we assert the entry point itself is correctly wired.
    """
    eps = [ep for ep in entry_points(group="continuo_runtime.adapters") if ep.name == "trino"]
    assert len(eps) == 1
    assert eps[0].load() is TrinoRuntimeAdapter
