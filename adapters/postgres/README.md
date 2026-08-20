# continuo-python-runtime-postgres

Postgres runtime-adapter library for Continuo python nodes. Implements
`continuo_engine_contract.port.RuntimeAdapter` (from the
`continuo-engine-contract` package) and registers itself under entry-point
group `continuo_runtime.adapters` as `postgres`.

Connection env: `POSTGRES_HOST`, `POSTGRES_DB`, `POSTGRES_USER` (required);
`POSTGRES_PORT` (default 5432), `POSTGRES_PASSWORD` (default empty).

`load()` and `ensure_table()` run transactionally (`autocommit = False`);
`fetch()` executes a single read and commits (or rolls back on error), never
leaving an open transaction dangling.

Verification tier: unit tests are mock-free pure-logic tests (type-grammar
validation, Arrow table construction from rows); DDL/transactional behavior
(schema/table creation, load truncate+insert+rollback) is verified against a
live postgres:16 engine in `tests/test_integration_runtime_postgres.py`.
