"""Python-node data-plane behaviours against a real DuckLake, plus the csv node end to end."""
import concurrent.futures
from datetime import date, datetime
from decimal import Decimal

import duckdb
import pyarrow as pa
import pytest
import yaml

from continuo_python_runtime.harness import run_node

pytestmark = pytest.mark.integration

BUCKET = "warehouse"
ID = [{"name": "id", "type": "INTEGER", "nullable": True}]


def test_ensure_table_creates_a_typed_table_with_not_null(adapter, schema, columns_of):
    adapter.ensure_table(
        schema, "typed",
        [{"name": "id", "type": "BIGINT", "nullable": False},
         {"name": "label", "type": "VARCHAR(10)", "nullable": True},
         {"name": "amount", "type": "NUMERIC(10,2)", "nullable": False}],
        config={},
    )
    assert columns_of(schema, "typed") == [
        ("id", "BIGINT", "NO"), ("label", "VARCHAR", "YES"), ("amount", "DECIMAL(10,2)", "NO"),
    ]


def test_ensure_table_is_idempotent(adapter, schema, columns_of):
    adapter.ensure_table(schema, "again", ID, config={})
    adapter.ensure_table(schema, "again", ID, config={})
    assert columns_of(schema, "again") == [("id", "INTEGER", "YES")]


def test_ensure_table_does_not_touch_an_existing_tables_data(adapter, schema, scalar):
    adapter.ensure_table(schema, "keep", ID, config={})
    adapter.load(schema, "keep", pa.table({"id": pa.array([1, 2, 3], pa.int32())}))
    adapter.ensure_table(schema, "keep", ID, config={})
    assert scalar(f'SELECT count(*) AS n FROM "{schema}"."keep"') == 3


def test_concurrent_ensure_table_all_callers_succeed(adapter_factory, schema, columns_of):
    def call(_):
        adapter_factory().ensure_table(schema, "race", ID, config={})

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(call, range(8)))
    assert columns_of(schema, "race") == [("id", "INTEGER", "YES")]


def test_fetch_round_trips_every_contract_type(adapter, schema):
    adapter.ensure_table(
        schema, "typed",
        [{"name": "i", "type": "BIGINT", "nullable": False},
         {"name": "d", "type": "NUMERIC(10,2)", "nullable": True},
         {"name": "dt", "type": "DATE", "nullable": True},
         {"name": "ts", "type": "TIMESTAMP", "nullable": True},
         {"name": "b", "type": "BOOLEAN", "nullable": True},
         {"name": "s", "type": "TEXT", "nullable": True}],
        config={},
    )
    data = pa.table({
        "i": pa.array([1, 2], pa.int64()),
        "d": pa.array([Decimal("1.50"), None], pa.decimal128(10, 2)),
        "dt": pa.array([date(2026, 1, 2), None], pa.date32()),
        "ts": pa.array([datetime(2026, 1, 2, 3, 4, 5), None], pa.timestamp("us")),
        "b": pa.array([True, None]),
        "s": pa.array(["x", None]),
    })
    adapter.load(schema, "typed", data)
    out = adapter.fetch(f'SELECT * FROM "{schema}"."typed" ORDER BY i')
    assert out.to_pylist() == data.to_pylist()


def test_fetch_rejects_duplicate_select_columns(adapter):
    with pytest.raises(ValueError, match="duplicate column name"):
        adapter.fetch("SELECT 1 AS id, 2 AS id")


def test_fetch_of_an_empty_result_keeps_the_declared_shape(adapter, schema):
    adapter.ensure_table(schema, "empty", ID, config={})
    out = adapter.fetch(f'SELECT id FROM "{schema}"."empty"')
    assert out.num_rows == 0 and out.schema.names == ["id"]


def test_load_replaces_contents_atomically(adapter, adapter_factory, schema, scalar):
    adapter.ensure_table(schema, "t", ID, config={})
    adapter.load(schema, "t", pa.table({"id": pa.array([1, 2, 3], pa.int32())}))

    # Observe the table from a second connection in the window between the delete
    # and the insert: a transactional replace must still show the old contents there.
    observer = adapter_factory()
    seen_mid_load: list[list[dict]] = []
    gateway = adapter._gateway
    real_insert = gateway.insert_arrow

    def insert_then_peek(table, data):
        seen_mid_load.append(observer.fetch(f'SELECT id FROM "{schema}"."t" ORDER BY id').to_pylist())
        real_insert(table, data)

    gateway.insert_arrow = insert_then_peek
    try:
        adapter.load(schema, "t", pa.table({"id": pa.array([9], pa.int32())}))
    finally:
        gateway.insert_arrow = real_insert

    assert seen_mid_load == [[{"id": 1}, {"id": 2}, {"id": 3}]]
    assert adapter.fetch(f'SELECT id FROM "{schema}"."t"').to_pylist() == [{"id": 9}]


def test_load_of_zero_rows_just_clears(adapter, schema, scalar):
    adapter.ensure_table(schema, "t", ID, config={})
    adapter.load(schema, "t", pa.table({"id": pa.array([1], pa.int32())}))
    adapter.load(schema, "t", pa.table({"id": pa.array([], pa.int32())}))
    assert scalar(f'SELECT count(*) AS n FROM "{schema}"."t"') == 0


def test_a_failed_load_leaves_prior_contents_intact(adapter, schema):
    adapter.ensure_table(schema, "t", [{"name": "id", "type": "INTEGER", "nullable": False}], config={})
    adapter.load(schema, "t", pa.table({"id": pa.array([1, 2], pa.int32())}))
    with pytest.raises(duckdb.Error):  # NOT NULL violated part-way through the insert
        adapter.load(schema, "t", pa.table({"id": pa.array([5, None], pa.int32())}))
    assert adapter.fetch(f'SELECT id FROM "{schema}"."t" ORDER BY id').to_pylist() == [{"id": 1}, {"id": 2}]
    adapter.load(schema, "t", pa.table({"id": pa.array([7], pa.int32())}))  # connection still usable


def test_load_accepts_an_all_null_column(adapter, schema):
    adapter.ensure_table(
        schema, "t", [{"name": "id", "type": "INTEGER", "nullable": True}, {"name": "note", "type": "TEXT", "nullable": True}],
        config={},
    )
    adapter.load(schema, "t", pa.table({"id": pa.array([1, 2], pa.int32()), "note": pa.nulls(2)}))
    assert adapter.fetch(f'SELECT id, note FROM "{schema}"."t" ORDER BY id').to_pylist() == [
        {"id": 1, "note": None}, {"id": 2, "note": None},
    ]


def test_awkward_identifiers_round_trip(adapter, schema):
    cols = [{"name": 'order"id', "type": "BIGINT", "nullable": False},
            {"name": "pct%", "type": "TEXT", "nullable": True},
            {"name": "order", "type": "TEXT", "nullable": True}]
    for table in ("50% order table", "select"):
        adapter.ensure_table(schema, table, cols, config={})
        adapter.load(schema, table, pa.table({
            'order"id': pa.array([7], pa.int64()), "pct%": ["x"], "order": ["y"],
        }))
        rows = adapter.fetch(
            f'SELECT "order""id" AS oid, "pct%" AS p, "order" AS o FROM "{schema}"."{table}"'
        ).to_pylist()
        assert rows == [{"oid": 7, "p": "x", "o": "y"}]


def test_a_schema_named_like_the_catalog_alias_is_not_ambiguous(adapter):
    try:
        adapter.ensure_table("lake", "lake", ID, config={})
        adapter.load("lake", "lake", pa.table({"id": pa.array([1], pa.int32())}))
        assert adapter.fetch('SELECT id FROM "lake"."lake"."lake"').to_pylist() == [{"id": 1}]
    finally:
        adapter.drop_schema("lake")


def _csv_contract_dir(tmp_path, schema):
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "t.yml").write_text(yaml.safe_dump({"nodes": [{
        "schema": schema, "table": "orders_csv", "owner": "m", "schedule": "daily",
        "criticality": "SECONDARY", "kind": "python-csv",
        "reads": {"csv": f"s3://{BUCKET}/drops/orders.csv"},
        "output_columns": [
            {"name": "order_id", "type": "INTEGER", "nullable": False},
            {"name": "amount", "type": "DOUBLE PRECISION"},
        ],
    }]}))
    return tmp_path


def test_run_node_csv_kind_loads_a_minio_csv_into_duckdb(
    adapter, adapter_factory, schema, s3, columns_of, monkeypatch, tmp_path,
):
    """The full production path: real minio csv -> harness -> DuckDBAdapter -> real DuckLake."""
    s3.put_object(Bucket=BUCKET, Key="drops/orders.csv", Body=b"order_id,amount\n1,10.5\n2,20.0\n3,5.25\n")
    monkeypatch.setenv("S3_ENDPOINT_URL", s3.meta.endpoint_url)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "minioadmin")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "minioadmin")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    repo = _csv_contract_dir(tmp_path, schema)
    env = {
        "NODE_ID": f"python-csv.svc.{schema}.orders_csv",
        "TABLE_NAME": "orders_csv",
        "TARGET_SCHEMA": schema,
        "CONTRACT_DIR": str(repo / "contracts"),
        "APP_ROOT": str(repo),
    }
    # run_node closes the adapter it is given, so hand it its own and verify through `adapter`.
    assert run_node(env, adapter=adapter_factory()) == 0
    assert columns_of(schema, "orders_csv") == [("order_id", "INTEGER", "NO"), ("amount", "DOUBLE", "YES")]
    rows = adapter.fetch(f'SELECT order_id, amount FROM "{schema}"."orders_csv" ORDER BY order_id').to_pylist()
    assert rows == [
        {"order_id": 1, "amount": 10.5}, {"order_id": 2, "amount": 20.0}, {"order_id": 3, "amount": 5.25},
    ]
