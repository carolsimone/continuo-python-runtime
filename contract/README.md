# continuo-engine-contract

The contract shared by the Continuo python runtime — the node harness and the
validation runner alike — and every engine adapter library. Seven modules:

| Module | What it holds |
|---|---|
| `continuo_engine_contract.port` | The `WarehouseAdapter` port — one port covering both the data plane (`fetch`/`ensure_table`/`load`) and validation (engine-specific empty-table DDL) — the opt-in write capabilities (`TableIntrospection`, `AppendCapable`, `UpsertCapable`, with `LiveColumn`), plus entry-point discovery |
| `continuo_engine_contract.keys` | `ensure_unique_key` and `DuplicateKeyError`: the primary-key check `UpsertCapable.upsert` runs before any engine sees the rows |
| `continuo_engine_contract.types` | The SQL column-type grammar that DDL-emitting methods validate against |
| `continuo_engine_contract.sql` | `ensure_single_read`, the parse-backed gate `check_binds` runs before it lets a declared read near an engine |
| `continuo_engine_contract.config` | `ensure_known_keys`, the fail-closed gate `build_empty_from_columns` runs before it lets a node's physical-layout `config` block reach DDL |
| `continuo_engine_contract.result` | The sentinel-framed result-block wire format the runner emits and the k8s-controller parses |
| `continuo_engine_contract.conformance` | The pytest conformance suite for the write capabilities — `IntrospectionConformance`, `AppendConformance`, `UpsertConformance` — that an adapter's integration tests subclass, providing `adapter` and `schema` fixtures. Needs the `testing` extra |

Each `continuo-python-runtime-<engine>` library depends on this package and
implements the port.

`ensure_single_read` is why this package has a runtime dependency —
[sqlglot](https://pypi.org/project/sqlglot/), pinned exactly — where it
previously had none. Deciding "is this one read query?" means parsing it, and
that rule is engine-independent: every adapter needs the same one, so it lives
here rather than being reimplemented per engine. Adapters call it with their
own dialect (`"postgres"`, `"trino"`, …) and it raises `ValueError` on
anything that is not exactly one query — no sqlglot exception escapes, so
callers never import sqlglot to handle a rejected read.

## A contract, not a service

This package runs nothing of its own — it isn't a service and it isn't the Job
either. It's the interface both callers agree on: `continuo-python-runtime`
(the node harness and the validation runner that call it) and every
`continuo-python-runtime-<engine>` adapter package (which implements it). The
whole contract is the twelve `WarehouseAdapter` methods (`required_env`,
`from_env`, `ensure_schema`, `drop_schema`, `build_empty_from_sql`,
`clone_empty_from_prod`, `build_empty_from_columns`, `check_binds`, `fetch`,
`ensure_table`, `load`, `close`) plus the sentinel result-block format, both
reached through the single `continuo_engine.adapters` entry-point group that
`discover_adapter` resolves against — and the three shared guards
implementations are required to run rather than reinvent:
`types.validate_column_type` before interpolating a type into DDL,
`sql.ensure_single_read` before letting a declared read reach an engine, and
`config.ensure_known_keys` before building any DDL from a node's
physical-layout `config` block, to reject keys this engine doesn't recognize.
Three capability interfaces are opt-in on top of the port. An adapter that
also inherits `AppendCapable` (`table_columns`, `append`) or `UpsertCapable`
(`table_columns`, `_upsert`; the port's own `upsert` checks key uniqueness
first) can run `append` / `upsert` nodes; an adapter that inherits neither
still runs every `replace` node. `ensure_table` takes an optional
keyword-only `primary_key`, passed only for upsert nodes.

Because the port *is* the contract, this package is very unlikely to need
changes — new warehouse support is a new adapter package implementing the
existing methods, not a change here. Touching this file means bumping a
version that the co-located runtime and adapters in this workspace consume
immediately, and that external adapter authors pin from PyPI, so treat it as
a deliberate, coordinated change. The `result` module is stricter still: its
wire format is frozen against Continuo's Go parser (`pkg/validationresult`)
and must not change from this side at all.
