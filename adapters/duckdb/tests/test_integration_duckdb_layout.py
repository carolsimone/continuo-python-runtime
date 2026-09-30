"""partitioned_by / sorted_by are really applied: catalog metadata AND Parquet files."""
import io

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

pytestmark = pytest.mark.integration

COLS = [
    {"name": "id", "type": "INTEGER", "nullable": True},
    {"name": "ts", "type": "TIMESTAMP", "nullable": True},
    {"name": "name", "type": "VARCHAR(20)", "nullable": True},
]

_PARTITION_COLUMNS = """
SELECT pc.partition_key_index, c.column_name, pc.transform
FROM ducklake_partition_column pc
JOIN ducklake_partition_info pi ON pi.partition_id = pc.partition_id AND pi.table_id = pc.table_id
JOIN ducklake_table t ON t.table_id = pc.table_id
JOIN ducklake_schema s ON s.schema_id = t.schema_id
JOIN ducklake_column c ON c.table_id = pc.table_id AND c.column_id = pc.column_id
WHERE s.schema_name = %s AND t.table_name = %s AND pi.end_snapshot IS NULL
  AND t.end_snapshot IS NULL AND c.end_snapshot IS NULL
ORDER BY pc.partition_key_index
"""
_ACTIVE_PARTITION_IDS = """
SELECT pi.partition_id FROM ducklake_partition_info pi
JOIN ducklake_table t ON t.table_id = pi.table_id
JOIN ducklake_schema s ON s.schema_id = t.schema_id
WHERE s.schema_name = %s AND t.table_name = %s AND pi.end_snapshot IS NULL AND t.end_snapshot IS NULL
"""
_SORT_EXPRESSIONS = """
SELECT se.expression, se.sort_direction, se.null_order
FROM ducklake_sort_expression se
JOIN ducklake_sort_info si ON si.sort_id = se.sort_id AND si.table_id = se.table_id
JOIN ducklake_table t ON t.table_id = se.table_id
JOIN ducklake_schema s ON s.schema_id = t.schema_id
WHERE s.schema_name = %s AND t.table_name = %s AND si.end_snapshot IS NULL AND t.end_snapshot IS NULL
ORDER BY se.sort_key_index
"""


def _partition_keys(cur, schema, table):
    cur.execute(_PARTITION_COLUMNS, (schema, table))
    return [(row[1], row[2]) for row in cur.fetchall()]


def _active_partition_ids(cur, schema, table):
    cur.execute(_ACTIVE_PARTITION_IDS, (schema, table))
    return sorted(row[0] for row in cur.fetchall())


def _sort_keys(cur, schema, table):
    cur.execute(_SORT_EXPRESSIONS, (schema, table))
    return [(row[0].strip('"'), row[1], row[2]) for row in cur.fetchall()]


def _parquet_files(s3, schema, table) -> dict[str, pa.Table]:
    """Every Parquet data file DuckLake wrote for the table, read back from MinIO."""
    listing = s3.list_objects_v2(Bucket="warehouse", Prefix=f"lake/{schema}/{table}/")
    files = {}
    for item in listing.get("Contents", []):
        if item["Key"].endswith(".parquet"):
            body = s3.get_object(Bucket="warehouse", Key=item["Key"])["Body"].read()
            files[item["Key"]] = pq.read_table(io.BytesIO(body))
    return files


# --- partitioned_by: catalog metadata ---------------------------------------


@pytest.mark.parametrize("build", ["build_empty_from_columns", "ensure_table"])
def test_partition_keys_reach_the_catalog(adapter, schema, catalog_db, build):
    config = {"partitioned_by": [
        "name",
        {"column": "id", "transform": "bucket", "buckets": 4},
        {"column": "ts", "transform": "month"},
    ]}
    if build == "ensure_table":
        adapter.ensure_table(schema, "t", COLS, config=config)
    else:
        adapter.ensure_schema(schema)
        adapter.build_empty_from_columns(schema, "t", COLS, config)
    assert _partition_keys(catalog_db, schema, "t") == [
        ("name", "identity"), ("id", "bucket(4)"), ("ts", "month"),
    ]


@pytest.mark.parametrize("transform", ["year", "month", "day", "hour"])
def test_every_time_transform_reaches_the_catalog(adapter, schema, catalog_db, transform):
    adapter.ensure_table(schema, "t", COLS, config={"partitioned_by": [{"column": "ts", "transform": transform}]})
    assert _partition_keys(catalog_db, schema, "t") == [("ts", transform)]


def test_time_transforms_work_on_a_date_column(adapter, schema, catalog_db):
    adapter.ensure_table(
        schema, "t", [{"name": "d", "type": "DATE", "nullable": True}],
        config={"partitioned_by": [{"column": "d", "transform": "month"}]},
    )
    assert _partition_keys(catalog_db, schema, "t") == [("d", "month")]


# --- partitioned_by: physical files -----------------------------------------


def test_partitioned_data_lands_in_one_directory_per_value(parquet_adapter, schema, s3):
    parquet_adapter.ensure_table(schema, "events", COLS, config={"partitioned_by": ["name"]})
    parquet_adapter.load(schema, "events", pa.table({
        "id": pa.array([1, 2, 3, 4, 5], pa.int32()),
        "ts": pa.array([None] * 5, pa.timestamp("us")),
        "name": ["a", "a", "a", "b", "b"],
    }))
    files = _parquet_files(s3, schema, "events")
    rows_per_directory: dict[str, int] = {}
    for key, table in files.items():
        directory = key.split("/")[-2]
        rows_per_directory[directory] = rows_per_directory.get(directory, 0) + table.num_rows
    assert rows_per_directory == {"name=a": 3, "name=b": 2}


# --- sorted_by: catalog metadata --------------------------------------------


@pytest.mark.parametrize("build", ["build_empty_from_columns", "ensure_table"])
def test_sort_keys_reach_the_catalog(adapter, schema, catalog_db, build):
    config = {"sorted_by": [{"column": "id", "direction": "desc", "nulls": "last"}, "name"]}
    if build == "ensure_table":
        adapter.ensure_table(schema, "t", COLS, config=config)
    else:
        adapter.ensure_schema(schema)
        adapter.build_empty_from_columns(schema, "t", COLS, config)
    assert _sort_keys(catalog_db, schema, "t") == [
        ("id", "DESC", "NULLS_LAST"), ("name", "ASC", "NULLS_LAST"),
    ]


def test_explicit_nulls_first_reaches_the_catalog(adapter, schema, catalog_db):
    adapter.ensure_table(schema, "t", COLS, config={"sorted_by": [{"column": "id", "nulls": "first"}]})
    assert _sort_keys(catalog_db, schema, "t") == [("id", "ASC", "NULLS_FIRST")]


# --- sorted_by: physical files ----------------------------------------------


@pytest.mark.parametrize("direction,nulls,expected", [
    ("asc", "first", [None, 1, 2, 3]),
    ("asc", "last", [1, 2, 3, None]),
    ("desc", "first", [None, 3, 2, 1]),
    ("desc", "last", [3, 2, 1, None]),
])
def test_rows_are_ordered_inside_the_parquet_file(parquet_adapter, schema, s3, direction, nulls, expected):
    parquet_adapter.ensure_table(
        schema, "t", COLS, config={"sorted_by": [{"column": "id", "direction": direction, "nulls": nulls}]}
    )
    parquet_adapter.load(schema, "t", pa.table({
        "id": pa.array([2, None, 1, 3], pa.int32()),
        "ts": pa.array([None] * 4, pa.timestamp("us")),
        "name": ["a"] * 4,
    }))
    files = list(_parquet_files(s3, schema, "t").values())
    assert len(files) == 1
    assert files[0].column("id").to_pylist() == expected


def test_partitioned_and_sorted_together(parquet_adapter, schema, catalog_db, s3):
    parquet_adapter.ensure_table(schema, "t", COLS, config={
        "partitioned_by": ["name"], "sorted_by": [{"column": "id", "direction": "desc"}],
    })
    parquet_adapter.load(schema, "t", pa.table({
        "id": pa.array([1, 3, 2, 8, 9], pa.int32()),
        "ts": pa.array([None] * 5, pa.timestamp("us")),
        "name": ["a", "a", "a", "b", "b"],
    }))
    assert _partition_keys(catalog_db, schema, "t") == [("name", "identity")]
    assert _sort_keys(catalog_db, schema, "t") == [("id", "DESC", "NULLS_LAST")]
    by_directory = {key.split("/")[-2]: table.column("id").to_pylist()
                    for key, table in _parquet_files(s3, schema, "t").items()}
    assert by_directory == {"name=a": [3, 2, 1], "name=b": [9, 8]}


# --- fail closed ------------------------------------------------------------


@pytest.mark.parametrize("config", [
    {"indexes": [{"columns": ["id"]}]},                              # postgres vocabulary
    {"sortkey": ["id"]},
    {"partitioned_by": ["nope"]},
    {"partitioned_by": [{"column": "id", "transform": "month"}]},    # time transform on INTEGER
    {"partitioned_by": [{"column": "id", "transform": "week"}]},
    {"partitioned_by": [{"column": "id", "transform": "bucket"}]},
    {"sorted_by": [{"column": "id", "direction": "sideways"}]},
    {"sorted_by": [{"column": "id + 1"}]},
    {"sorted_by": ["id", "id"]},
])
@pytest.mark.parametrize("build", ["build_empty_from_columns", "ensure_table"])
def test_a_bad_layout_raises_and_leaves_no_table(adapter, schema, tables_in, config, build):
    adapter.ensure_schema(schema)
    with pytest.raises(ValueError):
        if build == "ensure_table":
            adapter.ensure_table(schema, "t", COLS, config=config)
        else:
            adapter.build_empty_from_columns(schema, "t", COLS, config)
    assert tables_in(schema) == []


def test_an_empty_config_applies_no_layout(adapter, schema, catalog_db):
    adapter.ensure_table(schema, "t", COLS, config={})
    assert _partition_keys(catalog_db, schema, "t") == []
    assert _sort_keys(catalog_db, schema, "t") == []


# --- idempotence and rebuild ------------------------------------------------


def test_a_second_ensure_table_with_the_same_layout_changes_nothing(adapter, schema, catalog_db):
    config = {"partitioned_by": ["name"], "sorted_by": ["id"]}
    adapter.ensure_table(schema, "t", COLS, config=config)
    before = _active_partition_ids(catalog_db, schema, "t")
    adapter.ensure_table(schema, "t", COLS, config=config)
    assert _active_partition_ids(catalog_db, schema, "t") == before
    assert len(before) == 1


def test_ensure_table_does_not_change_the_layout_of_an_existing_table(adapter, schema, catalog_db):
    adapter.ensure_table(schema, "t", COLS, config={"partitioned_by": ["name"]})
    adapter.ensure_table(schema, "t", COLS, config={"partitioned_by": [{"column": "ts", "transform": "month"}]})
    assert _partition_keys(catalog_db, schema, "t") == [("name", "identity")]


def test_a_rebuild_replaces_the_previous_layout(adapter, schema, catalog_db):
    adapter.ensure_schema(schema)
    adapter.build_empty_from_columns(schema, "t", COLS, {"partitioned_by": ["name"]})
    adapter.build_empty_from_columns(
        schema, "t", COLS, {"partitioned_by": [{"column": "ts", "transform": "month"}], "sorted_by": ["id"]}
    )
    assert _partition_keys(catalog_db, schema, "t") == [("ts", "month")]
    assert len(_active_partition_ids(catalog_db, schema, "t")) == 1
    assert _sort_keys(catalog_db, schema, "t") == [("id", "ASC", "NULLS_LAST")]
    adapter.build_empty_from_columns(schema, "t", COLS, {})
    assert _partition_keys(catalog_db, schema, "t") == []
    assert _sort_keys(catalog_db, schema, "t") == []
