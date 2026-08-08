# Continuo Python Runtime

Runtime harness, contract tooling, and CI lint for Continuo python nodes.
This repo is what domain data teams (marketing, finance, …) template from to
ship a python node into Continuo: write a contract + a `run(ctx)` script,
push to `main`, and CI does the rest — lint, validate, merge, build, publish,
and register the release with Continuo.

## What this repo is

Three artifacts come out of this repository:

- **The `continuo-python-runtime` PyPI package** — the `continuo-runtime` CLI
  (`validate` / `merge` / `hash` / `lint` / `run`) and the harness library
  (`conform()`, `RunContext`, the error taxonomy) that domain repos install.
- **Per-engine base images**, one per warehouse engine (`...-postgres`,
  `...-trino`), that domain repos build `FROM`. Each image bakes in the
  runtime, a single `RuntimeAdapter` for that engine, and the
  `continuo-runtime run` entrypoint.
- **`template/`** — a copy-ready domain repo: `Dockerfile`, `contracts/`,
  `scripts/`, and the `release.yml` CI/CD workflow.

Base images and the PyPI publication of this package land with this repo's
PR 9 (the image pipeline); until then, install the runtime from git as noted
in `template/.github/workflows/release.yml`.

### Package map

Each per-engine base image bakes together three PyPI packages: the harness,
the engine adapter, and the published contract. The harness and both engine
adapters live in *this* repo (`continuo-python-runtime`) as a uv workspace;
`continuo-validation-runners` owns the *validation-side* (offline lint/merge)
counterparts of the same engines, which is a separate concern from the
data-plane adapters here.

| Package (distribution name) | Module | Lives in | Role |
| --- | --- | --- | --- |
| `continuo-python-runtime` | `continuo_python_runtime` | this repo (root) | Harness: CLI, `conform()`, `RunContext`, error taxonomy. |
| `continuo-python-runtime-postgres` | `continuo_python_runtime_postgres` | this repo, `python-runtime-postgres/` | Data-plane `RuntimeAdapter` for Postgres (`fetch`/`ensure_table`/`load`). **Not published to PyPI** — built from source into the image. |
| `continuo-python-runtime-trino` | `continuo_python_runtime_trino` | this repo, `python-runtime-trino/` | Data-plane `RuntimeAdapter` for Trino/Iceberg. **Not published to PyPI** — built from source into the image. |
| `continuo-validation-contract` | `continuo_validation_contract` | `continuo-validation-runners` | The published contract (schema, `RuntimeAdapter` port, result-block format) both sides depend on. |
| `continuo-validation-postgres` / `continuo-validation-trino` | `continuo_validation_postgres` / `continuo_validation_trino` | `continuo-validation-runners` | Validation-side (lint/merge, no live warehouse I/O) adapters — not to be confused with the data-plane adapters above. |

All three packages built in this repo resolve `continuo-validation-contract`
from PyPI (`==0.6.0`); the two adapter packages are uv workspace members
(`[tool.uv.workspace]` in the root `pyproject.toml`), so `uv sync
--all-packages --all-groups` at the repo root installs everything for local
development.

**Only `continuo-python-runtime` is published to PyPI.** The two engine
adapters are built **from source into the runtime images**: `Dockerfile.postgres`
and `Dockerfile.trino` `pip install` them out of the build context, so each
image ships exactly one adapter and the harness discovers it through the
`continuo_runtime.adapters` entry-point group at run time. Nothing installs
them from an index — the harness package does not depend on them, and domain
repos get their adapter by building `FROM` a published base image. They are
still built, type-checked, and tested by CI on every change.

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
parser (sqlglot, via `continuo_validation_contract.sql.ensure_single_read`)
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
  `PyYAML`, and `continuo-validation-contract`. Add whatever you script
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
FROM ghcr.io/carolsimone/continuo-python-runtime:v0.2.0-postgres
# or
FROM ghcr.io/carolsimone/continuo-python-runtime:v0.2.0-trino
```

Each image bakes in exactly one `RuntimeAdapter` for that engine — installed
from this repo's `python-runtime-postgres/` or `python-runtime-trino/`
package (see the package map above; both adapters live in this repo, not
`continuo-validation-runners`) — registered under the
`continuo_runtime.adapters` entry-point group (entry names `postgres` /
`trino`). The harness discovers it via `discover_runtime_adapter()` at run
time, so a single image serves every node in the service. The executor
injects the
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
