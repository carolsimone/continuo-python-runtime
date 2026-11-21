# Continuo Python Runtime

[![CI](https://github.com/carolsimone/continuo-python-runtime/actions/workflows/ci.yml/badge.svg)](https://github.com/carolsimone/continuo-python-runtime/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/carolsimone/continuo-python-runtime?label=release)](https://github.com/carolsimone/continuo-python-runtime/releases)

Runtime harness, contract tooling, and CI lint for Continuo python nodes.
This repo is what domain data teams (marketing, finance, …) template from to
ship a python node into Continuo: write an ODCS contract and a `run(ctx)`
script, push to `main`, and CI does the rest — lint, validate, build and push
the image, pack the contracts, upload the bundle and register the release with
Continuo.

## What this repo is

Five artifacts come out of this repository:

- **The `continuo-python-runtime` PyPI package** — the `continuo-runtime` CLI
  (`validate` / `pack` / `hash` / `packages` / `lint` / `run` /
  `validation-op`) and the harness library (`conform()`, `RunContext`, the
  error taxonomy) that domain repos install.
- **The `continuo-engine-contract` PyPI package** — the `WarehouseAdapter`
  port, the contract schema, the shared SQL/type/config guards, and the
  sentinel result-block format. Adapter authors outside this repo pin it.
- **The three engine-adapter PyPI packages** (`continuo-postgres-adapter`,
  `continuo-trino-adapter`, `continuo-duckdb-adapter`) — one `WarehouseAdapter` implementation per
  warehouse engine, each published independently under the same tag. A
  domain repo normally never installs these directly (the engine image
  already has the matching one baked in); they exist as standalone PyPI
  packages for the "build your own container" shape (see below) and for
  third-party adapter authors to reference.
- **Per-engine base images**, one per warehouse engine
  (`continuo-python-runtime-postgres`, `continuo-python-runtime-trino`,
  `continuo-python-runtime-duckdb`), that
  domain repos build `FROM`. Each image bakes in the runtime and a single
  `WarehouseAdapter` for that engine, and serves both roles that adapter has:
  the node harness (`ENTRYPOINT ["continuo-runtime"]`, `CMD ["run"]`) and the
  validation runner (`continuo-runtime validation-op`).
- **`template/`** — a copy-ready domain repo: `Dockerfile`, `contracts/`,
  `scripts/`, and the `release.yml` CI/CD workflow.

One `vX.Y.Z` git tag releases all of it: `publish-pypi.yml` builds all five
PyPI distributions into a single `dist/`, verifies them (and full engine
images built from them) on amd64 and arm64, and only then publishes them
together. It then builds all three engine images natively per architecture —
each installing its matching pinned adapter version from that same release —
pulls and smoke-tests the exact pushed digests, and finally gives the verified
amd64 + arm64 digests one multi-arch tag. See `CONTRIBUTING.md` (Releasing).

### What this repo owns

This repository owns the entire python-node surface: the engine contract, the three
engine adapters, the validation runner, and the node harness. There is no
separate validation-side port, adapter class, entry-point group or image: one
`WarehouseAdapter` per engine serves both the data plane (`fetch` /
`ensure_table` / `load`, plus the opt-in `table_columns` / `append` / `upsert`
capabilities) and validation (`ensure_schema` / `drop_schema` /
`build_empty_from_sql` / `build_empty_from_columns` / `clone_empty_from_prod` /
`check_binds`), and one image per engine runs both roles.

| Package (distribution name) | Module | Lives in | Role |
| --- | --- | --- | --- |
| `continuo-python-runtime` | `continuo_python_runtime` | this repo (root) | Harness (CLI, `conform()`, `RunContext`, error taxonomy) **and** the validation runner (`continuo-runtime validation-op`). Published to PyPI. |
| `continuo-engine-contract` | `continuo_engine_contract` | this repo, `contract/` | The `WarehouseAdapter` port, contract schema, the SQL/type/config guards adapters must run, and the result-block format. Published to PyPI. |
| `continuo-postgres-adapter` | `continuo_postgres_adapter` | this repo, `adapters/postgres/` | `PostgresAdapter` — one class, both roles. Published to PyPI. |
| `continuo-trino-adapter` | `continuo_trino_adapter` | this repo, `adapters/trino/` | `TrinoAdapter` — one class, both roles, for Trino/Iceberg. Published to PyPI. |
| `continuo-duckdb-adapter` | `continuo_duckdb_adapter` | this repo, `adapters/duckdb/` | `DuckDBAdapter` — one class, both roles, for DuckDB on a DuckLake (Postgres catalog, Parquet on S3). Published to PyPI. |

All five are uv workspace members (`[tool.uv.workspace]` in the root
`pyproject.toml`), so `uv sync --all-packages --all-groups` at the repo root
installs everything for local development.

**All five packages in the table above are published to PyPI**, under the
same `vX.Y.Z` tag. The three engine images then **install the matching pinned
adapter version from PyPI** — `Dockerfile.postgres` installs
`continuo-postgres-adapter==X.Y.Z`, `Dockerfile.trino` installs
`continuo-trino-adapter==X.Y.Z` and `Dockerfile.duckdb` installs
`continuo-duckdb-adapter==X.Y.Z` — rather than building it from this repo's
source tree, so each image still ships exactly one adapter and the runtime
still discovers it through the `continuo_engine.adapters` entry-point group
at run time. The image **name** (`continuo-python-runtime-<engine>`) and the
adapter's pip **distribution** name (`continuo-<engine>-adapter`) are two
different artifacts of the same adapter — same engine, same version, same
runtime behavior, different packaging; see "Build your own container" below
for a build shape that installs the pip package directly instead of `FROM`
the image. All five packages are still built, type-checked, and tested by CI
on every change.

### The result block is a frozen wire contract

`continuo_engine_contract.result` writes a sentinel-framed JSON block as the
last line of stdout, and Continuo's Go side parses it byte-for-byte
(`pkg/validationresult` in the `continuo` repository). That wire format — the
sentinel markers, the framing, and the field names inside the block — is
**frozen**. The additions are an optional `reason` key, written after
`unique_id` only when an outcome has a machine-readable code (for example
`image_contract_mismatch`), and the op's own keys that the `seed_preflight` and
`probe_views` ops write after `reason`; a block without either keeps its bytes,
and the sentinels, `schema_version` and the five standard keys do not change. Reuse
it; never change it from this side alone. A change here that
the Go parser has not been taught is a production outage, not a refactor.

## Quickstart for domain teams

1. Copy `template/` into a new repository.
2. Edit `template/.github/workflows/release.yml` and set `SERVICE` to your
   service name (one service name per domain repo).
3. Configure repository variables in GitHub (Settings → Secrets and
   variables → Actions): `REGISTRY` (your Docker registry), `BUCKET` (your
   S3 bucket for the contract bundle), `RELEASE_ENDPOINT` (the base
   URL of your continuo install, `scheme://host[:port]` with no path, the origin of continuo's
   `auth.publicUrl`). The
   workflow calls `<RELEASE_ENDPOINT>/api/v1/releases` with its GitHub Actions
   OIDC token, so the repository must be bound to your service in continuo's
   `ciAuth.bindings` (see [Releasing from CI](https://github.com/carolsimone/continuo/blob/main/deploy/README.md#releasing-from-ci-github-actions)),
   and an operator bootstraps the service's first release.
4. Configure repository secrets: `AWS_ACCESS_KEY_ID` and
   `AWS_SECRET_ACCESS_KEY` for the S3 upload. The template workflow pushes
   the built image to GHCR using the workflow's own `GITHUB_TOKEN` (granted
   `packages: write`) — no registry secret is needed for that. If you point
   `REGISTRY` at a different or private registry, add your own `docker
   login` step to `release.yml`.
5. Write one ODCS contract per node under `contracts/`, named
   `<table>.odcs.yaml` (see `template/contracts/example.odcs.yaml` and
   "Contract files" below), and a script under `scripts/` that implements
   `run(ctx)` (see `template/scripts/example.py`). A node that only needs to
   land a csv file needs no script at all — see
   `template/contracts/example_csv.odcs.yaml` and "Node kinds" below.
6. Push to `main`. The `release.yml` workflow lints the scripts, validates
   the contracts, builds and pushes the image, packs the contracts into
   `odcs-bundle.json`, uploads it to S3, and POSTs the release with
   `"kind": "python-odcs"`.

## Contract files

Each node has one contract: an ODCS v3.2.0 data contract in `contracts/`,
named after its table, `<table>.odcs.yaml`, or `<schema>.<table>.odcs.yaml`
when two nodes of one service share a table name. The `continuo-contracts`
library reads each file strictly (YAML 1.2 core, with no aliases, no duplicate
keys and one document per file) and checks it. `validate`, `pack`, `hash` and
the harness load the folder the same way; the harness skips the read-query
checks.

```yaml
apiVersion: v3.2.0
kind: DataContract
id: analytics.example          # <schema>.<table>, in lower case
name: example                  # the table
version: "0"                   # continuo numbers published versions itself; this value is ignored
description:
  purpose: Replace with your node
team:
  name: your-team              # the team that owns the node
customProperties:
  - {vendor: continuo, property: schedule, value: daily}
  - {vendor: continuo, property: criticality, value: SECONDARY}
  - {vendor: continuo, property: script, value: scripts/example.py}
  - vendor: continuo
    property: readQueries
    value:
      orders: select order_id, customer_id, amount from analytics.upstream_orders
      customers: select customer_id, name from analytics.upstream_customers
schema:
  - name: example
    physicalType: table
    properties:
      - {name: order_id, physicalType: INTEGER, required: true}
      - {name: customer_name, physicalType: "VARCHAR(64)"}
      - {name: amount, physicalType: "NUMERIC(10,2)", required: true}
```

- `id` is `<schema>.<table>` in lower case; `name` and the schema object's
  `name` are the table.
- `team.name` is the node's owner and `description.purpose` its description.
- continuo's own settings are `customProperties` items with
  `vendor: continuo`. Every node sets `schedule` and `criticality`
  (`REGULATORY`, `CORE` or `SECONDARY`); the node's kind decides the rest
  (see "Node kinds" below). An optional `service` names the node's service,
  and `pack` and `validate --service` refuse a contract whose `service` is not
  their `--service`.
- A contract may also bind its table and its columns to the company's ontology
  (see "Checking against the ontology" below).
- The contract has exactly one schema object, `physicalType: table`. Its
  `properties` are the table's columns, in order: each has a `name`, a
  `physicalType` (`BIGINT`, `INTEGER` or `INT`, `DOUBLE PRECISION`,
  `NUMERIC(p,s)` or `DECIMAL(p,s)`, `VARCHAR(n)`, `CHAR(n)`, `TEXT`,
  `TIMESTAMP`, `DATE` or `BOOLEAN`) and, for a column that never holds NULL,
  `required: true`.

`validate` refuses any other file in `contracts/` whose name ends in `.yml` or
`.yaml`, in any case, two files that declare the same table, and a folder with
no contract. `docs/boundary-contract.md` lists every field a contract may
carry, and how a contract in another format converts to ODCS.

### Read queries

`validate`, `pack` and `hash` hand every read query to a real SQL parser
(sqlglot, through `continuo_engine_contract.sql.ensure_single_read`). Each
query must be one `SELECT` (or `WITH`) statement, and every table it reads
must name its schema: `analytics.orders`, never `orders`. The one name that
goes without a schema is a reference to a CTE that is in scope where the name
is used, so in `WITH orders AS (SELECT * FROM orders) SELECT * FROM orders` the
`orders` inside the CTE is a table and must name its schema. continuo refuses,
when it resolves a release, a read query that names a table without its schema,
so CI refuses at least what the release refuses.

SQL a driver accepts but a parser refuses fails these checks, most often a
driver's bind placeholder such as psycopg2's `%(name)s`, which `ctx.read(name)`
could never fill. Engine-specific syntax (postgres `~`, `@>`, …) needs
`--dialect <engine>`, which a repo should pass anyway, since Continuo parses
and bind-checks every read in the install's own warehouse dialect:

```bash
continuo-runtime validate contracts/ --dialect postgres   # or trino, duckdb
```

The harness does not run these read checks again when a node starts, so a
read that passes here is not judged again under a different grammar in
production. See `docs/boundary-contract.md` §13.1.

### Checking against the ontology

A company that keeps an ontology file, the things its data is about and the
services that own them, can have each contract bind its table and its columns
to it. The bindings are optional `continuo` properties: `entity`, `grain`,
`fixed` and `filter` on the schema object, and `property`, `local` and
`aggregate` on a column (`docs/boundary-contract.md` gives their values):

```yaml
schema:
  - name: example
    physicalType: table
    customProperties:
      - {vendor: continuo, property: entity, value: Order}
    properties:
      - name: order_id
        physicalType: INTEGER
        required: true
        customProperties:
          - {vendor: continuo, property: property, value: Order.order_id}
      - name: note
        physicalType: "VARCHAR(64)"
        customProperties:
          - {vendor: continuo, property: local, value: true}
```

If `Order` has a key part or a property named `order_id`, the check would bind
that column itself; the binding is written out for clarity.

A contract with bindings loads, validates, packs and runs like any other, and
its `content_hash` is the one it has without them. To check the bindings in your
CI, keep a copy of the company's ontology file in your own repository, for
example at `ontology/ontology.yaml`, and give `validate` the name of your
service and the path of that copy:

```bash
continuo-runtime validate contracts --service <name> --ontology ontology/ontology.yaml
```

Copy the file from the company's ontology repository, never from continuo, and
keep it outside `contracts/`, where `validate` refuses any other YAML file. The
copy is yours to keep current: when the ontology changes, you update it. A copy
that is behind the ontology can pass here and still be refused by the release,
which checks against the ontology the install runs. The library's dbt check,
`continuo-contracts dbt check`, takes the same file with `--ontology`.

`--service` alone refuses a contract whose `service` property names another
service. `--ontology` needs `--service`; without it `validate` exits 2. With
both, every contract is checked as a contract of that service by the rules of
`continuo-contracts` that need no other service: `contract`, `coverage`,
`quality` and `privacy`. The rules that read the whole estate (`access`,
`order` and `drift`) cannot run on one service's contracts and are not run
here. `validate` logs, on stderr and at the level of its severity (ERROR,
WARNING or INFO), one line for each finding,
`<severity> [<rule>] <subject>: <message>`, then one INFO line that counts them,
`<e> error(s), <w> warning(s), <i> info: <name>`. It exits 1 when any finding is
an error and 0 otherwise, and it prints nothing to stdout.

- When the ontology declares the service, each contract names exactly one of
  `entity` or `grain`, so a contract that names neither, or both, is an error.
  A column that carries no binding is a warning, unless its name is a key part
  or a property of the table's entity, in which case the check binds it itself.
- When the ontology does not declare the service, none of its contracts is
  checked: `validate` logs one INFO line, `not declared in the ontology: none
  of this service's contracts is checked`, and one WARNING for each contract
  that writes bindings, and exits 0.
- A file that cannot be read, is not UTF-8 text or is not an ontology is one
  ERROR line, `<file>: <reason>`, and exit 1. The reason of an invalid
  ontology is the library's, with the path to the first fault.
- A file that is an ontology but that the library's `ontology` rule refuses (a
  thing owned by a domain the file does not declare, a service of a domain it
  does not declare, a personal property no access rule names) is refused the
  same way, whatever the service, and no contract is checked against it: one
  ERROR line, `<file>: the ontology file has <n> error(s): run
  continuo-contracts ontology check <file>`, and exit 1. That command lists the
  errors. Findings of that rule that are not errors do not stop the check.

A company without an ontology adds nothing: without the two flags `validate`
runs the checks above and no others.

## Node kinds

The `kind` property (a `customProperties` item, not the contract's root
`kind: DataContract`) selects how the node produces its rows.
Every rule below (`extraColumns`, the columns, "Conform rules") applies to
every kind identically — `kind` only changes how the pre-conform table is
produced, never how it is checked or written.

- **`python-node`** (the default; the property may be left out) — a script
  node. It needs `script` and `readQueries`, a mapping of one or more named
  SQL queries, as described in "The script API" below.
- **`python-csv`** — a contract-only node: it has no script and reads no
  table. Its `csvSource` is the file's address, `s3://bucket/key` or an
  `https://` url (`http://` is refused by `validate`, not at run time).
  The harness fetches the file, parses it with RFC 4180 defaults, and feeds
  the result straight into `conform()` — the declared column types decide
  the warehouse schema, not whatever pyarrow infers from the csv. Because
  there is no script, `script`, `readQueries`, `secretRef` and `assets` are
  refused on this kind. The csv's header row must contain every declared
  column (checked again, independently, at release time before promotion);
  columns present in the header but not declared are governed by the same
  `extraColumns` policy as a script node's output — `raise` (default) fails
  the run, `warn` drops them and logs a warning. See
  `template/contracts/example_csv.odcs.yaml`.
- **`python-api`** — a script node that fetches its own data (typically over
  HTTP) and declares no `readQueries`. `ctx.read` has nothing to return. It
  may name one Kubernetes Secret with `secretRef: continuo-api-<name>`;
  continuo attaches that Secret to the node's pod, so each of its keys is an
  env var the script reads with `os.environ[...]`. The contract never holds a
  secret value or env var name; the Secret itself is created in the cluster
  by the operator.

### Files a script reads (`assets`)

A `python-node` or `python-api` script that reads a file from the repository
(a model, a lookup table, a JSON config) lists it in the `assets` property,
one path relative to the repository root per entry, written with forward
slashes and with no empty, `.` or `..` part:

```yaml
customProperties:
  - {vendor: continuo, property: script, value: scripts/scored_orders.py}
  - vendor: continuo
    property: assets
    value: [data/rates.json, models/scorer.bin]
```

`pack` reads each listed file and folds its bytes into `shared_code_hash`
under its path, so editing one changes the node's `content_hash` and starts a
validation. A path that is also an import of the script is one unit. `pack`
and `hash` fail with a `ContractError` for a path that is missing, is not a
file or resolves outside the repository, and `validate` refuses a path listed
twice. The image must `COPY` the files, because `assets` only tells the hash
about them. A `python-csv` node has no script, so `assets` is refused on it.

### Task timeout

Any node may set `timeoutMinutes` — an integer from 1 to 10,080 (one week) —
to declare how long its task may run before continuo gives up on it. A value
outside that range, or one that is not an integer, fails `validate`, `pack`
and `hash`. Like the owner (`team.name`), `schedule`, `criticality` and the
description, it is outside the code projection that `config_hash` hashes, so
changing it never changes the node's `content_hash`.

## Write modes

The `modeType` property chooses how the harness writes a node's table on each
run:

| `modeType` | Each run | Rows from earlier runs |
| --- | --- | --- |
| `replace` (default) | empties the table, inserts this run's rows | gone |
| `append` | inserts this run's rows | kept |
| `upsert` | inserts rows with a new key, overwrites rows whose key exists | kept unless overwritten; never deleted |

```yaml
customProperties:
  - {vendor: continuo, property: kind, value: python-api}
  - {vendor: continuo, property: modeType, value: upsert}
schema:
  - name: fx_rates
    physicalType: table
    properties:
      - {name: rate_date, physicalType: DATE, required: true, primaryKey: true, primaryKeyPosition: 1}
      - {name: base, physicalType: "VARCHAR(3)", required: true, primaryKey: true, primaryKeyPosition: 2}
      - {name: quote, physicalType: "VARCHAR(3)", required: true, primaryKey: true, primaryKeyPosition: 3}
      - {name: rate, physicalType: "DECIMAL(12,6)"}
```

- `python-csv` nodes only `replace`.
- An `upsert` node needs a key: `primaryKey: true` on each key column. Each
  key column is `required: true` (a NULL key never matches) and is not
  `DOUBLE PRECISION`; a key of several columns numbers its columns with
  `primaryKeyPosition`, from 1. The key's names are unique
  case-insensitively. On a `replace` or `append` node a key only describes
  the table: the harness writes no key and the hash leaves it out. A
  Postgres `replace` table that wants uniqueness uses `indexes` with
  `unique: true` in its `config` property.
- Every write is atomic: all of the run's rows or none. An `append` or
  `upsert` run with zero rows writes nothing (a `replace` run with zero rows
  still empties the table).
- `append` is a plain insert: a run that committed but was reported failed
  and is retried stores its rows twice. `upsert` is idempotent.
- An `upsert` run whose own rows repeat a key fails with `ConformError` and
  writes nothing.
- Before an `append` or `upsert` the harness compares the live table with
  the contract's columns — names case-insensitively, nullability exactly; order and
  types are not compared — and fails with `LoadError` naming every missing
  column, unexpected column and nullability mismatch. Changing an `append` or
  `upsert` table (columns, types, nullability, key or mode) needs a new table
  name (`_v2`) under a new contract; continuo rejects the release otherwise.
  Adding a nullable column is no exception: the existing rows cannot be
  backfilled.
- The installed engine must support the mode; all three shipped adapters do.
  A bring-your-own adapter declares `AppendCapable` / `UpsertCapable` from
  `continuo-engine-contract` (and can run its conformance suite); without them
  an `append`/`upsert` node fails with `LoadError: <Adapter> does not support
  <mode>` before its script runs.
- Engines: on Postgres the key is the table's `PRIMARY KEY`, created with the
  table; DuckLake and Trino/Iceberg have no constraints, so the key is
  guaranteed by the write path only. On Trino an `upsert` table needs Iceberg
  `format_version` 2 (the default).
- `full_refresh` stays refused for every python node: a rebuild would destroy
  an accumulating table.

## The script API

A node script is a Python file with exactly one required entry point:

```python
def run(ctx):
    ...
```

- `ctx` is a `RunContext` (`continuo_python_runtime.context.RunContext`).
  Its only method is `ctx.read(name)`, where `name` is one of the query
  names of the contract's `readQueries` — reading anything else raises
  `ReadError`. Each declared read is fetched once and
  memoized; `ctx.read(name)` returns a `pyarrow.Table`.
- `run(ctx)` can return anything Arrow-convertible: a `pyarrow.Table`
  as-is, a pandas `DataFrame` (converted via
  `pa.Table.from_pandas(..., preserve_index=False)`), or any object
  implementing the Arrow C stream protocol (`__arrow_c_stream__`) — for
  example a polars DataFrame. Returning anything else raises `ScriptError`.
- The dataframe library is your choice. No dataframe library is baked into
  the base image — the package's own runtime dependencies are `pyarrow`,
  `PyYAML`, `continuo-engine-contract` and `continuo-contracts`. Add whatever you script
  against (pandas, polars, …) as a `RUN pip install` line in your own
  `Dockerfile`, on top of the base image. The base image runs as non-root
  (uid 65532), which pip cannot install under, so switch to root for the
  install and back afterward — `USER root`, then the `RUN pip install`
  line, then `USER 65532:65532` — since the executor's pod spec expects
  the image to end at uid 65532. See `template/Dockerfile` for the pattern.
- Scripts do not import warehouse drivers, write raw SQL literals, or call
  data-access methods directly — `continuo-runtime lint` rejects those:
  - forbidden driver imports (`psycopg2`/`sqlalchemy`/`trino`/etc.),
  - SQL string literals (in plain strings, f-strings, and `+` concatenation),
  - forbidden data-access calls (`execute`/`read_sql`/etc.), including ones
    reached via a `from ... import` alias (e.g. `from pandas import
    read_sql as rs` then calling `rs(...)`),
  - private/protected attribute access (`obj._x`) — except on `self`/`cls`,
    so a script's own class-private helpers (`self._helper()`) aren't
    flagged,
  - code the content hash cannot see. The hash folds the script and the files
    it imports, found by reading imports, so a node that runs anything else
    can change without its `content_hash` changing. Lint refuses `runpy`, a
    change to `sys.path` (`append`, `insert`, assignment, `del`, and
    `site.addsitedir`), a `sitecustomize` or `usercustomize` module (and a file
    of that name), `compile()` and `types.FunctionType`, and `pickle.load`,
    `pickle.loads` and `pickle.Unpickler`. `pack` applies the same rules to
    every file in a node's import closure, and refuses a `sitecustomize.py` or
    `usercustomize.py` at the repository root, which Python imports before the
    harness starts. A file a script reads, such as a model or a lookup table,
    belongs in the node's `assets`, where its bytes are hashed.

  The SQL-literal rule is best-effort: docstrings are exempt, but other
  prose may still occasionally match. The hard guarantees enforced by lint
  are the driver-import and data-access-call rules, together with the fact
  that `RunContext` only exposes `ctx.read()` — all warehouse access goes
  through it.
- Scripts may import shared in-repo helpers. Before executing a script the
  harness puts the repo root (`APP_ROOT`) and the script's own directory on
  `sys.path`, so both `import helpers` (a sibling of the script) and
  `from lib.shared import ...` (anywhere under the repo root) work, including
  from inside `run()`. Every helper a script reaches transitively is folded
  into `shared_code_hash`, so editing one re-fingerprints the node — but the
  hash does not put the file in the image: **`COPY` every directory your
  scripts import from in your `Dockerfile`**, or the release is valid and the
  node dies with `ModuleNotFoundError` on its first run. Because the repo root
  precedes the standard library on `sys.path`, avoid naming a top-level module
  after a stdlib one (`types.py`, `json.py`, `logging.py`, …).
- The harness — not the script — performs the write. It calls `conform()`
  on whatever `run()` returned and issues the only INSERT; the script never
  writes directly.

## Conform rules

`conform()` (`continuo_python_runtime/conform.py`) enforces the columns the
node's contract declares on the table `run()` returned, in this order:

| Check | Behavior |
| --- | --- |
| Duplicate columns | Any duplicate column name in the returned table always raises `ConformError`. |
| Extra columns | Governed by the contract's `extraColumns` policy: `raise` (default) fails the run; `warn` drops the undeclared column(s) and logs a warning. |
| Missing columns | Any declared column absent from the returned table always raises `ConformError`. |
| Column order | The table is reselected into the declared column order. |
| Strict cast | Each column is cast to its declared Arrow type with `safe=True`. Casts pyarrow's `safe=True` would silently accept but that are not value-lossless are rejected before the cast even runs: floating → decimal (rounds to scale), non-boolean → boolean (coerces truthiness), timestamp → date (drops time-of-day). Any other cast failure also raises `ConformError`. |
| Not-null | A column declared `required: true` that contains any null raises `ConformError`. |
| VARCHAR/CHAR length | A column declared `VARCHAR(n)`/`CHAR(n)` whose longest value exceeds `n` raises `ConformError`. |

## Error taxonomy

Every runtime failure is one of six `HarnessError` subclasses
(`continuo_python_runtime/errors.py`). The sentinel result block's message
is prefixed `<ErrorClass>: ` so failures can be triaged without parsing free
text. The harness also logs the same message at ERROR on stderr, with the
traceback of the exception it wraps (a driver error, the script's own
exception), so the stored log of a failed run names its failure: Continuo cuts
the result block out of the log before storing it.

| Class | Meaning | Typical fix target |
| --- | --- | --- |
| `ContractError` | Contract missing or invalid, node not found for `NODE_ID`, or the declared script is missing/unreachable. | The contract file or its `script` path. |
| `ImageContractMismatch` (a `ContractError`) | The image does not hold the contract, script, imports or assets the release hashed: the `content_hash` in `CONTENT_HASH` differs from the one the image's files give, or a file the release hashed is missing from the image. The block also carries `"reason":"image_contract_mismatch"`. | Rebuild the image from the released commit, with CI and the image on the same runtime. |
| `ReadError` | `ctx.read()` was called with an undeclared name, or a declared read failed at the warehouse. | The contract's `readQueries`, or the upstream query/warehouse access. |
| `ScriptError` | `run()` raised, has no callable `run`, or returned a value that isn't Arrow-convertible. | The node script. |
| `ConformError` | Structural mismatch (extra/missing/duplicate columns), a strict-cast failure, a not-null violation, a VARCHAR/CHAR overflow, or an `upsert` run whose rows repeat a primary key. | The script's output shape, or the contract's columns. |
| `LoadError` | Adapter construction failed, the installed engine does not support the node's `modeType`, the live table's shape differs from the contract's columns (append/upsert), or the DDL/INSERT failed at the warehouse during the write. | Warehouse connectivity/permissions, the engine image, or the target table. |

## Engine selection

A domain repo picks its warehouse engine by which base image it builds
`FROM`:

```dockerfile
FROM ghcr.io/carolsimone/continuo-python-runtime-postgres:v0.14.0
# or
FROM ghcr.io/carolsimone/continuo-python-runtime-trino:v0.14.0
# or
FROM ghcr.io/carolsimone/continuo-python-runtime-duckdb:v0.14.0
```

The engine is part of the image **name**; the tag is the bare version, so
Continuo's Helm chart can pin an image as `<name>:vX.Y.Z@sha256:<digest>`.

Each image bakes in exactly one `WarehouseAdapter` for that engine — the
pinned PyPI version of the `continuo-postgres-adapter`,
`continuo-trino-adapter` or `continuo-duckdb-adapter` package built from this
repo's `adapters/postgres/`, `adapters/trino/` or `adapters/duckdb/` source
(see the table above) — registered under the
`continuo_engine.adapters` entry-point group (entry names `postgres` /
`trino` / `duckdb`). The runtime discovers it via `discover_adapter()` at run time, so a
single image serves every node in the service and the release-time
validation Job for it. The executor injects the warehouse connection as
environment variables (engine-native, e.g.
`POSTGRES_HOST`/`POSTGRES_DB`/`POSTGRES_USER`) plus the node-selection
environment (`NODE_ID`, `TABLE_NAME`, `TARGET_SCHEMA`) that
`continuo-runtime run` reads to pick the node. `CONTRACT_DIR`, the folder of
the image's `*.odcs.yaml` contracts (`/app/contracts` by default), and
`APP_ROOT`, the repository root a node's `script` path is resolved against,
are optional.

### Build your own container

A domain repo does not have to build `FROM` the published engine image.
`template/` ships two Dockerfiles for the two build shapes (see
`template/README.md` § "Choosing a base" for the full comparison):

- **Shape 1 — `template/Dockerfile`** — `FROM` the published engine image
  (`continuo-python-runtime-<engine>`), as shown above. Simplest; the image
  already has the runtime and adapter installed and pinned.
- **Shape 2 — `template/Dockerfile.pip`** — your own base image, installing
  `continuo-python-runtime` and one `continuo-<engine>-adapter` from PyPI
  via a hash-locked `requirements.lock` (`template/requirements.lock`). Use
  this when you must control the base image yourself.

Both shapes end up running the same runtime and the same adapter version;
which one you pick only changes who controls the base OS layer underneath
them.

## Seed preflight and view probe

Two validation ops read the warehouse and change nothing. They run through the
same runner image as the other validation ops (`VALIDATION_OP`), need no
`DBT_TARGET_SCHEMA`, and print one result block as the last line of stdout.

A seed reload drops and recreates the seed's production table. On an engine where
a drop destroys the objects that read the table (`drop_semantics` `cascade`,
postgres) that takes every view, function and constraint built on the seed with
it, including objects continuo does not own. On an engine where those objects
survive and fail on their next read (`late_binding`, trino and duckdb) they
may go stale. `seed_preflight` lists what a reload would touch and compares it
with what the operator has allowed or acknowledged; `probe_views` tries named
views after a reload and says which no longer bind.

`seed_preflight` runs in two places. `check` runs in the seed task's own pod just
before dbt and stops the reload with exit 1 when the check concludes it must.
`report` runs in the release's seed-build Job and only reports what a `check`
would conclude; it exits 0 whatever it finds.

### `seed_preflight` inputs

| Variable | Meaning |
|---|---|
| `SEED_PREFLIGHT_MODE` | `check` or `report`. |
| `SEED_PREFLIGHT_ENGINE_FAMILY` | `schema_bound` or `late_binding`, the family the release plan expects of the engine. |
| `PROD_SCHEMA`, `TABLE_NAME` | The seed's production relation. |
| `SEED_PREFLIGHT_ALLOWED`, `SEED_PREFLIGHT_ACKNOWLEDGED` | JSON arrays of `"<schema>.<name>"`, default `[]`. An object is allowed when `<schema>.<name>`, in lower case, is in either list (entries are trimmed and lower-cased). |
| `NODE_ID` | Optional; the block's `unique_id`. Default `<PROD_SCHEMA>.<TABLE_NAME>`. |

The adapter's own variables (`POSTGRES_*`, `TRINO_*`, ...) are needed as for any
other op. An adapter's family is `schema_bound` when its `drop_semantics` is
`cascade`, and `late_binding` when it is `late_binding`.

### `seed_preflight` block

After the standard keys (and `reason` when one applies): `op` (`seed_preflight`),
`mode`, `checked` (the warehouse was asked and its listing came back), `engine_family` (as given),
`drop_semantics` (the adapter's), `completeness` (`exact`, `best_effort`, or
`none` when nothing was listed) and `dependents`, a list of
`{schema, name, kind, owner, depth, allowed}` sorted by depth, schema and name.
`status` is `error` for exit codes 1 and 2, and `failures` is 1 then.

`reason` is what a `check` concludes. `report` prints the same `reason` on a
`success` block.

| Situation | `reason` | `check` | `report` |
|---|---|---|---|
| a missing or malformed input, no adapter, missing adapter env | none | exit 2 | exit 2 |
| the adapter's family differs from `SEED_PREFLIGHT_ENGINE_FAMILY`, or it declares none | `engine_mismatch` | exit 1 | exit 0 |
| the adapter cannot list dependents, `schema_bound` | `unowned_dependents` | exit 1 | exit 0 |
| the adapter cannot list dependents, `late_binding` | none | exit 0 | exit 0 |
| the connection fails | `warehouse_unreachable` | exit 1 | exit 0 |
| the listing fails, `schema_bound` | none | exit 1 | exit 0 |
| the listing fails, `late_binding` | none | exit 0 | exit 0 |
| listed, `schema_bound`, an object is not allowed | `unowned_dependents` | exit 1 | exit 0 |
| anything else listed (on `late_binding` the objects not allowed are external views that may go stale, named in `message`) | none | exit 0 | exit 0 |

A seed table that does not exist yet has no dependents and passes. A stop's
`message` starts with its `reason` and names each object outside the set as
`<schema>.<name> (<kind>, owner <owner>)`.

### `probe_views`

`PROBE_VIEWS` is a JSON array of `"<schema>.<name>"`, split on the first dot;
`NODE_ID` is optional (default `probe_views`). Each view is read with
`SELECT * FROM "<schema>"."<name>" LIMIT 0`. The block has `op`
(`probe_views`), `checked` (the warehouse was reachable) and `views`, a list of
`{schema, name, ok, error}` in the order given, where `error` is the first line
of the failure, at most 500 characters, and empty when `ok`. A view that fails
its probe never fails the op: the exit code is 0, and `message` reads
`<n> of <total> views failed: <names>`. A warehouse that cannot be reached gives
exit 0, `checked` false, `views` empty and `reason` `warehouse_unreachable`.
Only a missing or malformed input, no adapter, missing adapter env or an
unexpected error is exit 2.

## Further reading

- `docs/boundary-contract.md` — the boundary between continuo and a domain
  repo (§13): the five surfaces (the contract bundle, the hashes `pack`
  computes, the release call, the runtime image, and the domain repo's CI)
  that this repo implements, and every field a contract may carry.
