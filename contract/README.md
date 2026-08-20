# continuo-engine-contract

The contract shared by the continuo validation runner and every engine adapter
library. Five modules:

| Module | What it holds |
|---|---|
| `continuo_engine_contract.port` | The `ValidationAdapter` port (engine-specific empty-table DDL) and the `RuntimeAdapter` port, plus entry-point discovery |
| `continuo_engine_contract.types` | The SQL column-type grammar that DDL-emitting methods validate against |
| `continuo_engine_contract.sql` | `ensure_single_read`, the parse-backed gate `check_binds` runs before it lets a declared read near an engine |
| `continuo_engine_contract.config` | `ensure_known_keys`, the fail-closed gate `build_empty_from_columns` runs before it lets a node's physical-layout `config` block reach DDL |
| `continuo_engine_contract.result` | The sentinel-framed result-block wire format the runner emits and the k8s-controller parses |

Each `continuo-validation-<engine>` library depends on this package and
implements the port.

`ensure_single_read` is why this package has a runtime dependency —
[sqlglot](https://pypi.org/project/sqlglot/), pinned exactly — where it
previously had none. Deciding "is this one read query?" means parsing it, and
that rule is engine-independent: every adapter needs the same one, so it lives
here rather than being reimplemented per engine. Adapters call it with their
own dialect (`"postgres"`, `"trino"`, …) and it raises `ValueError` on
anything that is not exactly one query — no sqlglot exception escapes, so
callers never import sqlglot to handle a rejected read.

## Job, not a service

This package runs nothing of its own — it isn't a service and it isn't the
Job either. It's the interface both sides of the validation Job agree on:
[`continuo-validation-runner`](https://github.com/carolsimone/continuo-validation-runners/blob/main/runner/README.md) (the Job that
calls it) and every `continuo-validation-<engine>` adapter package (which
implements it). The whole contract is the nine `ValidationAdapter` methods
(`required_env`, `from_env`, `ensure_schema`, `drop_schema`,
`build_empty_from_sql`, `clone_empty_from_prod`, `build_empty_from_columns`,
`check_binds`, `close`) plus the sentinel result-block format
`discover_adapter` resolves against at import time — and the three shared
guards implementations are required to run rather than reinvent:
`types.validate_column_type` before interpolating a type into DDL,
`sql.ensure_single_read` before letting a declared read reach an engine, and
`config.ensure_known_keys` before building any DDL from a node's
physical-layout `config` block, to reject keys this engine doesn't recognize.

Because the port *is* the contract, this package is very unlikely to need
changes — new warehouse support is a new adapter package implementing the
existing methods, not a change here. Touching this file means bumping a
version that the co-located runner and adapters in this workspace consume
immediately, and that external adapter authors pin from PyPI, so treat it as
a deliberate, coordinated change.
