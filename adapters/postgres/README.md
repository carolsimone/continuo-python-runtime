# continuo-postgres-adapter

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

`from_env` tries a connection that no server answered (every address of the
host refused, had no route or sat on an unreachable network) up to 5 times,
2 s apart, logging each retry: a Job pod's first connection can be refused
for a few seconds after the pod starts, until the cluster's NetworkPolicy
engine has admitted the pod. Every other failure, an authentication failure or
a connect timeout on any address among them, is raised at once.

The connection runs with `autocommit = True`; every method owns its
transaction explicitly. `load()` and `ensure_table()` open an explicit
`BEGIN` around their statements and `COMMIT`/`ROLLBACK` themselves, so a
mid-sequence failure leaves no partial change. `fetch()` executes a single
read and commits (or rolls back on error), never leaving an open transaction
dangling.

`PostgresAdapter` declares `drop_semantics = "cascade"` and is a
`DependentsIntrospection`: `dependents(schema, table)` lists every object a
`DROP TABLE ... CASCADE` of the table destroys or changes, with
`completeness="exact"`. It walks `pg_depend` from the table through every normal
dependency, so it lists views and materialized views (found through their
`_RETURN` rule, at any depth: a view of a view of the table is depth 2),
SQL-standard functions and procedures, foreign keys and policies on other
tables, rules, inheriting tables, tables with a column of the table's row type
or of an array of it, the types built on it (a domain, a range, a composite
type, a domain over an array of it) and what reads them, including a column of
the array or multirange type of such a type, a generated column over a dropped
column and what reads it, and what reads a partition of the table or a sequence
of it (owned or identity). A table that only loses a column is listed, and from
it only what reads that column is walked on.
`information_schema.view_table_usage` misses the materialized views, the
functions and every view past the first level. The table's own indexes,
constraints (a self-referencing foreign key included), policies, rules, column
defaults, sequences, partitions and triggers are parts of the table, and the
array and multirange types, constructor functions and casts a type internally
owns (the constructors of a range or multirange type and the range-to-multirange
cast) are parts of the type: none is listed. A cast a user created, such as
`CREATE CAST (seed AS text)`, is dropped by the cascade and is listed as `other`
(`cast from <schema>.seed to text`). Each object appears once, at its shallowest depth.
The walk follows at most 32 dependency levels; an object deeper than that is not
listed. The table is found case-insensitively, so a seed loaded under another
letter case never makes the walk skip it, and a schema holding two tables that
differ only by letter case lists the dependants of both; a missing table or
schema gives no objects. Each `kind` is one of `view`, `materialized_view`,
`table`, `function`, `procedure`, `constraint`, `policy`, `rule` or `other`.
`other` is anything else: a domain, a range type or another type (named
`type <name>`), a composite type (its own name), an index, a user cast, a column
default of another table (named `default value for column ...`) or a trigger.
A name that Postgres describes (a type, a cast, a column default) is
schema-qualified unless the object is visible on the connection's `search_path`
(a domain in `public` is named `type my_domain`). The
name of a function carries its argument types (`seed_count()`), and that of a
constraint, policy or rule the table or domain it sits on
(`orders_seed_id_fkey on orders`). A listed object that has no table of its own
(a type, a column default, a trigger) has an empty schema and an empty owner.
CI runs the dependents suite against the postgres:16 test stack; it was also
verified against a postgres:18.3 server by pointing the suite at it with
`VR_IT_PG_PORT` (host `localhost`, database `warehouse`, user and password
`continuo`).

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
