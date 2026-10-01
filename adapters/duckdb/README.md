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
| `DUCKDB_TEMP_DIRECTORY` | no | `.tmp` in the working directory | Where DuckDB spills larger-than-memory work; must be writable by the runtime user (the image sets `/tmp/duckdb-tmp`) |
| `DUCKDB_DATA_INLINING_ROW_LIMIT` | no | DuckLake default | `0` writes every insert as a Parquet file instead of inlining small ones in the catalog |

Settings are parsed strictly and fail fast with the variable named:

- `DUCKDB_S3_USE_SSL` accepts `true`/`false`/`1`/`0`/`yes`/`no` (any case);
  anything else is an error rather than silently meaning "true".
- `DUCKDB_S3_ACCESS_KEY_ID` and `DUCKDB_S3_SECRET_ACCESS_KEY` are set together or
  not at all; one without the other is rejected (it would otherwise fall back to
  the credential chain without saying so).
- `DUCKDB_CATALOG_PORT` and `DUCKDB_DATA_INLINING_ROW_LIMIT` take ASCII digits
  only.

## Credentials and failure modes

The catalog password is handed to libpq through a private (mode 0600) temporary
passfile, not in the connection string, so it does not appear in DuckDB's error
text or in `duckdb_databases()`. Every DuckDB error also passes through one
redaction point that masks the catalog password and the S3 secret (every
spelling) before the error reaches the result block or the pod logs; host, port
and database stay visible. The password travels inline instead (still redacted from errors) when it
contains a line break, when `PGPASSWORD` is set in the environment (libpq would
prefer it to any passfile), or when no temp file can be created. The `aws`
extension is only loaded for the AWS credential-chain S3 path.

A first attach to a brand-new catalog from several Jobs at once can race on
DuckLake's metadata creation; the adapter retries exactly that failure a few
times, and surfaces every other connection error immediately.

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

## Engine behaviour to know

- DuckDB drops the length of `VARCHAR(n)` / `CHAR(n)`: the catalog shows plain
  `VARCHAR`. Length is enforced by `conform()` for python nodes only, not by the
  table.
- DuckLake inlines small inserts into the catalog (see
  `DUCKDB_DATA_INLINING_ROW_LIMIT`), so partitioning and sorting are applied to
  Parquet files only after a flush; set the limit to `0` when files must be
  laid out per write.
- A read is one single query: top-level `PIVOT` / `UNPIVOT` statements are
  rejected by the single-read gate. Wrap them: `SELECT * FROM (PIVOT ...)`.

## Parity with the postgres and trino adapters

| Behaviour | postgres | trino | duckdb |
|---|---|---|---|
| `drop_schema` | `DROP SCHEMA IF EXISTS ... CASCADE` | same | same (tables and views go too) |
| `ensure_schema` under concurrency | session advisory lock | `IF NOT EXISTS`, tolerating a concurrent creation | `IF NOT EXISTS`, bounded retry on a DuckLake snapshot conflict |
| `check_binds` | `EXPLAIN` in `BEGIN READ ONLY` | `EXPLAIN (TYPE VALIDATE)` | `EXPLAIN` in `BEGIN TRANSACTION READ ONLY`, always rolled back |
| `ensure_table` | creates if absent, layout on create | same | same, in one transaction with the existence check |
| `load` (replace contents) | one transaction | atomic table swap (no multi-statement transactions) | one transaction: `DELETE` then `INSERT` |
| Physical layout keys | `indexes` | `partitioning`, `sorted_by`, `format`, `format_version` | `partitioned_by`, `sorted_by` |

Not applicable here: postgres `indexes`; trino `format` and `format_version`
(DuckLake always writes Parquet). The postgres advisory lock has no DuckLake
counterpart and is replaced by the bounded conflict retry; the trino table swap
is replaced by the single `DELETE` + `INSERT` transaction.

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
