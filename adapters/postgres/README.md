# continuo-python-runtime-postgres

Postgres engine-adapter library for Continuo python nodes. `PostgresAdapter`
implements `continuo_engine_contract.port.WarehouseAdapter` (from the
`continuo-engine-contract` package) — one class covering both the data plane
(`fetch` / `ensure_table` / `load`) and release-time validation
(`ensure_schema` / `drop_schema` / `build_empty_from_sql` /
`build_empty_from_columns` / `clone_empty_from_prod` / `check_binds`) — and
registers itself under entry-point group `continuo_engine.adapters` as
`postgres`.

Connection env: `POSTGRES_HOST`, `POSTGRES_DB`, `POSTGRES_USER` (required);
`POSTGRES_PORT` (default 5432), `POSTGRES_PASSWORD` (default empty).

The connection runs with `autocommit = True`; every method owns its
transaction explicitly. `load()` and `ensure_table()` open an explicit
`BEGIN` around their statements and `COMMIT`/`ROLLBACK` themselves, so a
mid-sequence failure leaves no partial change. `fetch()` executes a single
read and commits (or rolls back on error), never leaving an open transaction
dangling.

The `config:` index vocabulary is the union of the two vocabularies this
adapter inherited from the merge: `columns`, `unique`, `type`, and `name`,
with the strictest check from each side applied. The derived index name is
built from the table and the column list only — `type` and `unique` are not
folded into it, so two index entries on the same columns that differ only in
`type` (or only in `unique`) collide on that derived name and are rejected as
a duplicate. Give one of them an explicit `name:` when that is genuinely
intended.

Verification tier: unit tests are mock-free pure-logic tests (type-grammar
validation, Arrow table construction from rows); DDL/transactional behavior
(schema/table creation, load truncate+insert+rollback, validation DDL) is
verified against a live postgres:16 engine in
`tests/test_integration_runtime_postgres.py` and
`tests/test_integration_postgres_validation.py`.
