# continuo-duckdb-adapter

DuckDB warehouse adapter for Continuo, running on a **DuckLake**: the catalog
lives in Postgres and table data is Parquet on S3/MinIO, so every Kubernetes
Job shares one transactional warehouse. Implements
`continuo_engine_contract.port.WarehouseAdapter`; registered under the
`continuo_engine.adapters` entry-point group as `duckdb`.

## Environment

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `DUCKDB_CATALOG_HOST` | yes | | Postgres host holding the DuckLake catalog |
| `DUCKDB_CATALOG_PORT` | no | `5432` | |
| `DUCKDB_CATALOG_DB` | yes | | |
| `DUCKDB_CATALOG_USER` | yes | | |
| `DUCKDB_CATALOG_PASSWORD` | no | empty | |
| `DUCKDB_DATA_PATH` | yes | | Data location, e.g. `s3://bucket/lake/` (fixed once the catalog exists) |
| `DUCKDB_S3_ENDPOINT` | no | AWS | `host:port`, for MinIO and other S3-compatible stores |
| `DUCKDB_S3_ACCESS_KEY_ID` / `DUCKDB_S3_SECRET_ACCESS_KEY` | no | credential chain | Static credentials; omit to use the AWS credential chain |
| `DUCKDB_S3_REGION` | no | `us-east-1` | |
| `DUCKDB_S3_URL_STYLE` | no | `path` with an endpoint, else `vhost` | `path` or `vhost` |
| `DUCKDB_S3_USE_SSL` | no | `true` | |
| `DUCKDB_EXTENSION_DIRECTORY` | no | DuckDB default | Where `ducklake`, `postgres`, `httpfs` and `aws` (the credential chain) are loaded from (set in the image) |
| `DUCKDB_DATA_INLINING_ROW_LIMIT` | no | DuckLake default | `0` writes every insert as a Parquet file instead of inlining small ones in the catalog |

## Physical layout (`config`)

- `partitioned_by`: non-empty list of a column name, or
  `{column, transform, buckets}` with `transform` in `identity`, `bucket`
  (`buckets` required), `year`, `month`, `day`, `hour` (time transforms need a
  `DATE` or `TIMESTAMP` column).
- `sorted_by`: non-empty list of a column name, or
  `{column, direction: asc|desc, nulls: first|last}`.
- Columns must be declared in `output_columns`. Any other key, including
  postgres's `indexes`, is rejected before any DDL runs.
- Layout is applied when `ensure_table` creates the table; changing it on an
  existing table is a no-op. `build_empty_from_columns` (the release gate)
  always rebuilds.

## Layout of the code

`domain/` (pure rules) <- `application/` (use cases, `LakeGateway` port) <-
`infrastructure/` (DuckDB/DuckLake I/O, SQL rendering); `adapter.py` is the
composition root and the entry-point target.

## Tests

```bash
uv run pytest adapters/duckdb/tests -m "not integration" -v
docker compose -f tests/smoke/duckdb-stack/docker-compose.yml up -d --wait
uv run pytest adapters/duckdb/tests -m integration -v
docker compose -f tests/smoke/duckdb-stack/docker-compose.yml down -v
```
