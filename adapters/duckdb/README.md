# continuo-duckdb-adapter

DuckDB warehouse adapter for Continuo, running on a DuckLake: the catalog lives
in Postgres and table data is Parquet on S3/MinIO. Implements
`continuo_engine_contract.port.WarehouseAdapter`; registered under the
`continuo_engine.adapters` entry-point group as `duckdb`.
