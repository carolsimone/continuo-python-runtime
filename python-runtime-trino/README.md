# continuo-python-runtime-trino

Trino (Iceberg connector) runtime-adapter library for Continuo python nodes.
Implements `continuo_validation_contract.port.RuntimeAdapter` (from the
published `continuo-validation-contract` package) and registers itself under
entry-point group `continuo_runtime.adapters` as `trino`.

Connection env: `TRINO_HOST`, `TRINO_CATALOG` (required); `TRINO_PORT` (default
8080), `TRINO_USER` (default `continuo`), `TRINO_HTTP_SCHEME` (default `http`),
`TRINO_PASSWORD` (optional; requires `TRINO_HTTP_SCHEME=https`). Mirrors
`continuo_validation_trino.adapter.TrinoAdapter.from_env` exactly.

## No multi-statement transactions

Unlike the postgres runtime adapter, Trino/Iceberg has no multi-statement
transactions, so `load()` cannot be a single atomic TRUNCATE+INSERT. Two
atomic-replace primitives were verified live (Trino 483 + Iceberg REST catalog)
before choosing one — see the full writeup in `continuo_python_runtime_trino/adapter.py`'s
module docstring:

- `CREATE OR REPLACE TABLE t AS SELECT * FROM stage` is a single Iceberg
  metadata commit but **silently drops NOT NULL constraints** on the replaced
  table (verified live: a NULL insert into a "NOT NULL" column succeeds
  afterwards, and this connector/version does not support re-adding NOT NULL via
  `ALTER TABLE ... ALTER COLUMN ... SET NOT NULL`). Rejected for this reason.
- `CREATE TABLE stage (LIKE target INCLUDING PROPERTIES)` at a fresh sibling
  Iceberg location + populate + a two-statement `ALTER TABLE ... RENAME TO` swap
  preserves the target's exact columns, NOT NULL constraints, partitioning,
  format, and other connector properties (verified live). **This is what
  `load()` uses.**

The swap's exact atomicity guarantee: nothing under the target name is touched
until every row has been inserted into staging, so a failure before the swap
begins (e.g. a NOT-NULL-violating row) leaves the prior target contents
untouched. The two-statement rename swap itself is *not* single-statement
atomic — there is a brief window between the two renames during which the
target name refers to neither table. `load()` makes a best-effort recovery
attempt (rename the private old relation back) if the second rename raises, and
best-effort drops only a private staging relation created by that load. If
recovery itself fails, the logged private old relation remains available for
manual recovery because it contains the original target data.

`load()` assumes a single writer per table: Continuo's scheduler runs at most
one Job per node at a time. Concurrent `load()` calls against the same table are
unsupported because their target renames would race. Each load uses UUID-named
private swap relations, so distinct targets cannot collide with each other or
with legitimate user tables.

## NOT NULL is supported here

Verified live against Trino 483 + the Iceberg REST catalog: NOT NULL column
constraints in `CREATE TABLE` **are** supported and enforced by INSERT
(CONSTRAINT_VIOLATION on a NULL value). `ensure_table()` emits NOT NULL for
`nullable=False` columns, same as the postgres adapter.

## Type-name mapping

The contract's SQL-type grammar (copied verbatim from the postgres adapter, same
regex) admits spellings Trino does not recognize as type names: `TEXT`,
`DOUBLE PRECISION`, `NUMERIC(p,s)`. These are mapped to Trino's own spellings
(`VARCHAR`, `DOUBLE`, `DECIMAL(p,s)`) after the grammar guard has already
rejected anything injection-shaped. Every other grammar token is valid Trino DDL
unchanged.

## Identifier quoting

The trino DBAPI has no psycopg2-style `sql.Identifier`. Catalog, schema, table,
and column names are double-quoted by hand, with every embedded double quote
doubled. This preserves legal delimited identifiers such as `sales-prod`,
`order id`, and `has"quote` without allowing names to escape their quotes.

Verification tier: unit tests are mock-free pure-logic tests (type-grammar
validation, type-name mapping, identifier quoting, location derivation, Arrow
table construction from rows); DDL/swap behavior (schema/table creation,
load-owned swap relations, property and NOT NULL preservation, failure cleanup)
is verified against a live Trino 483 + Iceberg REST stack in
`tests/test_integration_runtime_trino.py`.
