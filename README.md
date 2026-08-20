# Continuo Python Runtime

Runtime harness, contract tooling, and CI lint for Continuo python nodes.
This repo is what domain data teams (marketing, finance, …) template from to
ship a python node into Continuo: write a contract + a `run(ctx)` script,
push to `main`, and CI does the rest — lint, validate, merge, build, publish,
and register the release with Continuo.

## What this repo is

Four artifacts come out of this repository:

- **The `continuo-python-runtime` PyPI package** — the `continuo-runtime` CLI
  (`validate` / `merge` / `hash` / `lint` / `run` / `validation-op`) and the
  harness library (`conform()`, `RunContext`, the error taxonomy) that domain
  repos install.
- **The `continuo-engine-contract` PyPI package** — the `WarehouseAdapter`
  port, the contract schema, the shared SQL/type/config guards, and the
  sentinel result-block format. Adapter authors outside this repo pin it.
- **Per-engine base images**, one per warehouse engine
  (`continuo-python-runtime-postgres`, `continuo-python-runtime-trino`), that
  domain repos build `FROM`. Each image bakes in the runtime and a single
  `WarehouseAdapter` for that engine, and serves both roles that adapter has:
  the node harness (`ENTRYPOINT ["continuo-runtime"]`, `CMD ["run"]`) and the
  validation runner (`continuo-runtime validation-op`).
- **`template/`** — a copy-ready domain repo: `Dockerfile`, `contracts/`,
  `scripts/`, and the `release.yml` CI/CD workflow.

One `vX.Y.Z` git tag releases all of it: `publish-pypi.yml` builds both
distributions into a single `dist/` and publishes them together, and
`images.yml` builds and pushes both engine images multi-arch under the same
tag.

### What this repo owns

This repository owns the entire python-node surface: the engine contract, both
engine adapters, the validation runner, and the node harness. The former
`continuo-validation` repository was merged in — there is no longer a separate
validation-side port, adapter class, entry-point group, or image. One
`WarehouseAdapter` per engine serves both the data plane (`fetch` /
`ensure_table` / `load`) and validation (`ensure_schema` / `drop_schema` /
`build_empty_from_sql` / `build_empty_from_columns` / `clone_empty_from_prod` /
`check_binds`), and one image per engine runs both roles.

| Package (distribution name) | Module | Lives in | Role |
| --- | --- | --- | --- |
| `continuo-python-runtime` | `continuo_python_runtime` | this repo (root) | Harness (CLI, `conform()`, `RunContext`, error taxonomy) **and** the validation runner (`continuo-runtime validation-op`). Published to PyPI. |
| `continuo-engine-contract` | `continuo_engine_contract` | this repo, `contract/` | The `WarehouseAdapter` port, contract schema, the SQL/type/config guards adapters must run, and the result-block format. Published to PyPI. |
| `continuo-python-runtime-postgres` | `continuo_python_runtime_postgres` | this repo, `adapters/postgres/` | `PostgresAdapter` — one class, both roles. **Not published to PyPI** — built from source into the image. |
| `continuo-python-runtime-trino` | `continuo_python_runtime_trino` | this repo, `adapters/trino/` | `TrinoAdapter` — one class, both roles, for Trino/Iceberg. **Not published to PyPI** — built from source into the image. |

All four are uv workspace members (`[tool.uv.workspace]` in the root
`pyproject.toml`), so `uv sync --all-packages --all-groups` at the repo root
installs everything for local development.

**Only `continuo-python-runtime` and `continuo-engine-contract` are published
to PyPI.** The two engine adapters are built **from source into the engine
images**: `Dockerfile.postgres` and `Dockerfile.trino` install them out of the
build context, so each image ships exactly one adapter and the runtime
discovers it through the `continuo_engine.adapters` entry-point group at run
time. Nothing installs them from an index — the harness package does not
depend on them, and domain repos get their adapter by building `FROM` a
published base image. They are still built, type-checked, and tested by CI on
every change.

### The result block is a frozen wire contract

`continuo_engine_contract.result` writes a sentinel-framed JSON block as the
last line of stdout, and Continuo's Go side parses it byte-for-byte
(`pkg/validationresult` in the `continuo` repository). That wire format — the
sentinel markers, the framing, and the field names inside the block — is
**frozen**. Reuse it; never change it from this side alone. A change here that
the Go parser has not been taught is a production outage, not a refactor.

## Quickstart for domain teams

1. Copy `template/` into a new repository.
2. Edit `template/.github/workflows/release.yml` and set `SERVICE` to your
   service name (one service name per domain repo).
3. Configure repository variables in GitHub (Settings → Secrets and
   variables → Actions): `REGISTRY` (your Docker registry), `BUCKET` (your
   S3 bucket for contract artifacts), `RELEASE_ENDPOINT` (the release
   webhook endpoint). `RELEASE_ENDPOINT` is the **base URL** of the Continuo
   API (no `/releases` suffix) — the workflow appends `/releases` itself.
4. Configure repository secrets: `AWS_ACCESS_KEY_ID` and
   `AWS_SECRET_ACCESS_KEY` for the S3 upload. The template workflow pushes
   the built image to GHCR using the workflow's own `GITHUB_TOKEN` (granted
   `packages: write`) — no registry secret is needed for that. If you point
   `REGISTRY` at a different or private registry, add your own `docker
   login` step to `release.yml`.
5. Write a contract file under `contracts/` (see
   `template/contracts/example.yml`) and a script under `scripts/` that
   implements `run(ctx)` (see `template/scripts/example.py`).
6. Push to `main`. The `release.yml` workflow lints the scripts, validates
   and merges the contracts, builds and pushes the image, uploads the merged
   contract to S3, and POSTs the release.

### Upgrading an existing domain repo

`validate` / `merge` / `hash` now hand every declared read to a real SQL
parser (sqlglot, via `continuo_engine_contract.sql.ensure_single_read`)
instead of scanning it for a leading `SELECT`/`WITH`. Two things follow for a
repo written before this, on its next release: SQL a driver would accept but
a parser will not — most commonly a driver-specific bind placeholder like
psycopg2's `%(name)s`, which `ctx.read(name)` could never have used anyway —
now fails validation, and engine-specific syntax (postgres `~`, `@>`, …)
needs `--dialect <engine>`, which a repo should be passing regardless since
Continuo bind-checks every read in the install's own warehouse dialect. Run
the pre-flight check once before your next release; it reports every affected
read at once:

```bash
continuo-runtime validate contracts/ --dialect postgres   # or trino
```

The runtime image does not re-run this gate, so a read that passes here is
not re-judged under a different grammar in production. See
`docs/boundary-contract.md` §13.1.

## The script API

A node script is a Python file with exactly one required entry point:

```python
def run(ctx):
    ...
```

- `ctx` is a `RunContext` (`continuo_python_runtime.context.RunContext`).
  Its only method is `ctx.read(name)`, where `name` is one of the read
  names declared under the node's `reads:` map in the contract — reading
  anything else raises `ReadError`. Each declared read is fetched once and
  memoized; `ctx.read(name)` returns a `pyarrow.Table`.
- `run(ctx)` can return anything Arrow-convertible: a `pyarrow.Table`
  as-is, a pandas `DataFrame` (converted via
  `pa.Table.from_pandas(..., preserve_index=False)`), or any object
  implementing the Arrow C stream protocol (`__arrow_c_stream__`) — for
  example a polars DataFrame. Returning anything else raises `ScriptError`.
- The dataframe library is your choice. No dataframe library is baked into
  the base image — the package's own runtime dependencies are `pyarrow`,
  `PyYAML`, and `continuo-engine-contract`. Add whatever you script
  against (pandas, polars, …) as a `RUN pip install` line in your own
  `Dockerfile`, on top of the base image.
- Scripts do not import warehouse drivers, write raw SQL literals, or call
  data-access methods directly — `continuo-runtime lint` rejects those:
  - forbidden driver imports (`psycopg2`/`sqlalchemy`/`trino`/etc.),
  - SQL string literals (in plain strings, f-strings, and `+` concatenation),
  - forbidden data-access calls (`execute`/`read_sql`/etc.), including ones
    reached via a `from ... import` alias (e.g. `from pandas import
    read_sql as rs` then calling `rs(...)`),
  - private/protected attribute access (`obj._x`) — except on `self`/`cls`,
    so a script's own class-private helpers (`self._helper()`) aren't
    flagged.

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

`conform()` (`continuo_python_runtime/conform.py`) enforces the node's
declared `output_columns` on the table `run()` returned, in this order:

| Check | Behavior |
| --- | --- |
| Duplicate columns | Any duplicate column name in the returned table always raises `ConformError`. |
| Extra columns | Governed by the node's `extra_columns` policy: `raise` (default) fails the run; `warn` drops the undeclared column(s) and logs a warning. |
| Missing columns | Any declared column absent from the returned table always raises `ConformError`. |
| Column order | The table is reselected into the declared column order. |
| Strict cast | Each column is cast to its declared Arrow type with `safe=True`. Casts pyarrow's `safe=True` would silently accept but that are not value-lossless are rejected before the cast even runs: floating → decimal (rounds to scale), non-boolean → boolean (coerces truthiness), timestamp → date (drops time-of-day). Any other cast failure also raises `ConformError`. |
| Not-null | A column declared `nullable: false` that contains any null raises `ConformError`. |
| VARCHAR/CHAR length | A column declared `VARCHAR(n)`/`CHAR(n)` whose longest value exceeds `n` raises `ConformError`. |

## Error taxonomy

Every runtime failure is one of five `HarnessError` subclasses
(`continuo_python_runtime/errors.py`). The sentinel result block's message
is prefixed `<ErrorClass>: ` so failures can be triaged without parsing free
text.

| Class | Meaning | Typical fix target |
| --- | --- | --- |
| `ContractError` | Contract missing or invalid, node not found for `NODE_ID`, or the declared script is missing/unreachable. | The contract yaml or the `script:` path. |
| `ReadError` | `ctx.read()` was called with an undeclared name, or a declared read failed at the warehouse. | The `reads:` map, or the upstream query/warehouse access. |
| `ScriptError` | `run()` raised, has no callable `run`, or returned a value that isn't Arrow-convertible. | The node script. |
| `ConformError` | Structural mismatch (extra/missing/duplicate columns), a strict-cast failure, a not-null violation, or a VARCHAR/CHAR overflow. | The script's output shape, or the `output_columns` declaration. |
| `LoadError` | Adapter construction failed, or the DDL/INSERT failed at the warehouse during the write. | Warehouse connectivity/permissions, or the target table. |

## Engine selection

A domain repo picks its warehouse engine by which base image it builds
`FROM`:

```dockerfile
FROM ghcr.io/carolsimone/continuo-python-runtime-postgres:v0.3.0
# or
FROM ghcr.io/carolsimone/continuo-python-runtime-trino:v0.3.0
```

The engine is part of the image **name**; the tag is the bare version, so
Continuo's Helm chart can pin an image as `<name>:vX.Y.Z@sha256:<digest>`.

Each image bakes in exactly one `WarehouseAdapter` for that engine — installed
from this repo's `adapters/postgres/` or `adapters/trino/` package (see the
table above) — registered under the `continuo_engine.adapters` entry-point
group (entry names `postgres` / `trino`). The runtime discovers it via
`discover_adapter()` at run time, so a single image serves every node in the
service and the release-time validation Job for it. The executor injects the
warehouse connection as environment variables (engine-native, e.g.
`POSTGRES_HOST`/`POSTGRES_DB`/`POSTGRES_USER`) plus the node-selection
environment (`NODE_ID`, `TABLE_NAME`, `TARGET_SCHEMA`, and optionally
`CONTRACT_DIR`/`APP_ROOT`) that `continuo-runtime run` reads to dispatch the
right node's script.

## Further reading

- `docs/superpowers/specs/2026-07-31-python-runtime-design.md` — this
  repo's design.
- `docs/boundary-contract.md` — the parent design's boundary contract (§13):
  the five surfaces (S3 artifact, `content_hash`, the release call, the
  runtime image, and the domain repo's CI/CD) that this repo implements.
