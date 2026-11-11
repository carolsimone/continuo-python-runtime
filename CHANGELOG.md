# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Every engine adapter declares `drop_semantics`, what dropping a table does
  to the objects that read it: `"cascade"` (they are destroyed) or
  `"late_binding"` (they survive and fail on their next read). The postgres
  adapter declares `cascade`, the trino and duckdb adapters `late_binding`.
  `StartupConformance` has a new case, `test_declares_a_drop_semantics`.
- `continuo_engine_contract.port` has an opt-in capability,
  `DependentsIntrospection`, with `Dependent` and `Dependents` and the
  vocabularies `DROP_SEMANTICS`, `COMPLETENESS` and `DEPENDENT_KINDS`. Its one
  method, `dependents(schema, table)`, lists every object a drop of the table
  would destroy or leave reading a missing table, and gives none for a missing
  table or schema. The conformance class `DependentsConformance` checks it,
  with an overridable `create_view` hook.
- `continuo-runtime --version` prints `continuo-runtime <version>` to stdout and
  exits 0. The template's `release.yml` has a "Pin the runtime to the image"
  step that reads the version from the Dockerfile's
  `FROM ghcr.io/carolsimone/continuo-python-runtime-<engine>:vX.Y.Z` line
  (failing with an `::error::` when none matches), installs exactly that
  runtime, and fails when `continuo-runtime --version` differs, so CI hashes
  with the runtime the image runs.
- The harness checks, before it builds the adapter, that the image holds what
  the release hashed. When `CONTENT_HASH` is set it recomputes the node's
  `content_hash` from the files in the image, with the helpers `merge` uses,
  and a difference, or a hashed file missing from the image, fails the node
  with `ImageContractMismatch` (a `ContractError`) and exit 1. The message
  names both hashes, the three parts, the unit paths and the runtime version.
  An unset or empty `CONTENT_HASH` logs a warning and runs. The result block
  carries an optional `reason` key after `unique_id`
  (`image_contract_mismatch`); a block without one keeps its bytes, and
  `continuo_engine_contract.result.result_block` takes it as a keyword-only
  `reason`.
- `assets:` on a `python-node` or `python-api` node lists the repository files
  its script reads. `merge` folds their bytes into `shared_code_hash`, so
  editing one changes the node's `content_hash`, and writes `assets` on the
  wire entry when a node declares any. A missing path, a non-file or a path
  that resolves outside the repository fails `merge`; `python-csv` refuses the
  key. The image must `COPY` the files.
- `lint` and `merge` refuse code the content hash cannot see: `runpy`, a change
  to `sys.path`, a `sitecustomize` or `usercustomize` module, `compile()` and
  `types.FunctionType`, and `pickle.load`, `pickle.loads` and
  `pickle.Unpickler`. `merge` also refuses a `sitecustomize.py` or
  `usercustomize.py` at the repository root. A file a script reads belongs in
  `assets`.

### Changed

- **Breaking for adapter authors.** `WarehouseAdapter.drop_semantics` is a
  required declaration with no default: an adapter that does not set it fails
  `StartupConformance.test_declares_a_drop_semantics`.
- A node's hashes come from continuo-contracts 0.1.0, the library
  continuo's topology-controller uses too, so CI and continuo compute the
  same values. `config_hash` hashes the node's code projection: `owner`,
  `description`, `criticality` and `schedule` no longer move a node's hash,
  a column type's spelling does not (`INT` and `INTEGER` hash alike), and a
  read's SQL is hashed as written, apart from LF line endings and the
  whitespace around it. `shared_code_hash` folds each import closure file
  with its repo-relative path, so two files that swap their contents change
  it. Every Python node's `content_hash` changes once, on the first release
  built with this version. The wire entry keeps its keys and the fold.
- A node whose `config` holds a value the hash cannot write (an integer
  beyond 2**53 - 1, NaN or an infinity, nesting deeper than 99 levels, a
  lone surrogate) fails `merge` with a `ContractError` naming the node.
- The package gate no longer counts `continuo-contracts` as a first-party
  distribution.

### Fixed

- A release's image build no longer fails when PyPI still serves an index
  page from before the upload. The engine images install their pins through
  `scripts/install_image_pins.py`, which retries an install while pip says a
  pinned version is not served yet (up to 10 minutes) and fails any other
  error at once. The v0.11.0 trino amd64 build hit this 40 s after the upload,
  although the runner's own probe had seen the new version. No package
  changes.

## [0.11.0] - 2026-11-04

**TL;DR**

- A failed python node's log now says why it failed.
- Every engine adapter waits up to about 10 s for a warehouse that refuses
  the first connections, as a new Kubernetes pod sees until the cluster's
  network policy admits it. That used to fail the attempt at once.
- The trino adapter now connects when it is created, so a wrong Trino
  password fails at start-up instead of at the first read.
- Adapter authors: `StartupConformance` is a new conformance class every
  adapter's integration suite runs.

**Upgrade**

1. Build your domain image `FROM` the `v0.11.0` engine image (or pin
   `continuo-python-runtime==0.11.0` and your adapter's new version).
2. Bring-your-own adapter: subclass `StartupConformance` in your integration
   suite and provide the `warehouse_address` and `adapter_from_env` fixtures.

Versions: runtime 0.11.0, engine contract 0.9.0, postgres adapter 0.3.2,
trino adapter 0.4.0, duckdb adapter 0.2.2.

### Fixed

- A failed python node's stored log names its failure. The harness logs every
  runtime error at ERROR on stderr, with the traceback of the exception it
  wraps, as well as printing it in the result block. Continuo cuts the result
  block out of the pod log before storing it, so a failure only printed there,
  such as `LoadError: adapter construction failed: … Connection refused`, was
  missing from the stored log.
- Every adapter's `from_env` waits out a warehouse that refuses connections
  for the first few seconds. A Job pod's first connection can be refused until
  the cluster's NetworkPolicy engine (k3s's kube-router rejects) has admitted
  the pod; that cost a task retry and a failed Job. A connection that no server
  answered (every address refused, had no route or sat on an unreachable
  network) is tried up to 5 times, 2 s apart, each retry logged. An
  authentication failure, a timeout on any address and every other failure are
  raised at once.
  - postgres: around `psycopg2.connect`.
  - duckdb: around the catalog attach.
  - trino: `from_env` now sends `SELECT 1`, since the trino DBAPI opens no
    connection until the first request. A refusal is retried only if it failed
    within 5 s: urllib3 reports only the last address it tried, so a slower
    one hides an address that timed out. A failed authentication now fails
    `from_env` (a harness `LoadError`) rather than the first read or write.

### Added

- `StartupConformance` in `continuo_engine_contract.testing.conformance`: every
  adapter's integration suite runs it. It puts the real engine behind a
  loopback port that refuses connections for 3 s (`LateOpeningPort`), then
  checks that `from_env` returns an adapter that answers `SELECT 1`. The new
  fixtures are `warehouse_address` and `adapter_from_env`.

### Changed

- The python-node env contract in `docs/boundary-contract.md` lists
  `TASK_ATTEMPT`, the 0-based number of the task's attempt, which the executor
  sets on every production Job. The harness does not read it; a node script
  may. Documentation only: no package changes.
- The engine images build on `python:3.14.8-slim` (was `3.14.2`), and
  `.python-version` and the `template/` Dockerfile follow.
- `pyarrow` is pinned at `26.0.0` (was `25.0.1`) in the runtime, the engine
  contract's `testing` extra and the three adapters, so they keep resolving
  together.
- Dependency pins move together across the workspace: `ruff` 0.17.1, `mypy`
  2.4.0, `boto3` 1.43.114, `sqlglot` 30.23.0 (runtime and engine contract),
  `trino` 0.340.0 and `psycopg2-binary` 2.9.13. `urllib3` moves to 2.8.0 in
  `uv.lock` (CVE-2026-97687, CVE-2026-97689).

## [0.10.0] - 2026-10-22

### Added

- Contracts may set `timeout_minutes` on any node: an integer from 1 to 10,080
  that is the node's task timeout. Other values are refused as a contract
  error naming the node. The merged wire entry carries the key only when set,
  and it is the one wire key outside `config_hash`: changing it alone leaves
  `config_hash` and `content_hash` unchanged. A node that does not set it has
  the wire entry it always had.

### Changed

- The "mode_type not allowed for kind" contract error now quotes the offending
  `mode_type` and `kind` (`repr`), matching the sibling "must be one of" error,
  so an empty or whitespace value is legible. Docstring fixes only alongside:
  the postgres `ensure_table` port-signature reference (0.6.0 → 0.8.0) and the
  `ensure_unique_key` message-order claim (pyarrow grouping order is not
  contractual).
- The trino adapter (0.3.1) and duckdb adapter (0.2.1) release with their
  `continuo-engine-contract` pin moved to 0.8.1; no behavior change.

## [0.9.0] - 2026-10-11

### Added

- Contracts may set `mode_type` — `replace` (the default), `append` or
  `upsert` — and, for `upsert` only, `primary_key`: a non-empty list of output
  columns, each spelled exactly as in `output_columns` and declared
  `nullable: false`, names unique case-insensitively, and none a
  floating-point (`DOUBLE PRECISION`) column — the warehouse collapses `0.0`
  and `-0.0`, so a float key is rejected at validation rather than failing at
  write time.
  `python-csv` nodes allow only `replace`. `continuo-runtime validate`, `merge`
  and `run` enforce these rules; the harness's write path for the new modes
  follows in a later entry.
- The merged wire entry carries `mode_type` when it is not `replace` and
  `primary_key` (authored order) for `upsert`; both are part of
  `config_hash`. Entries of nodes that do not set them, and their hashes, are
  unchanged.
- `continuo-engine-contract`: opt-in write capabilities on top of
  `WarehouseAdapter` — `TableIntrospection` (`table_columns`, returning
  `LiveColumn(name, nullable)`; `LookupError` for a missing table),
  `AppendCapable` (`append`) and `UpsertCapable` (`upsert`, which runs
  `ensure_unique_key` and then the engine's `_upsert`). `ensure_table` takes an
  optional keyword-only `primary_key`. `continuo_engine_contract.keys` holds
  `ensure_unique_key` and `DuplicateKeyError(ValueError)`.
- `continuo_engine_contract.testing.conformance`: a pytest conformance suite for the
  write capabilities (`IntrospectionConformance`, `AppendConformance`,
  `UpsertConformance`). Adapter integration suites subclass the reusable
  classes and run them against their real engine, providing `adapter` and
  `schema` fixtures. Install with `continuo-engine-contract[testing]`.
- `continuo-postgres-adapter`: `append` (batched inserts in one transaction),
  `upsert` (`INSERT ... ON CONFLICT (key) DO UPDATE`, `DO NOTHING` when every
  column is in the key) and `table_columns`; `ensure_table` declares an
  upsert key as the table's `PRIMARY KEY`. `load` now shares the insert path
  (no behavior change).
- `continuo-duckdb-adapter`: `append` (one transaction), `upsert` (`MERGE`
  from the registered Arrow data, one transaction) and `table_columns`.
  `ensure_table` accepts and ignores an upsert key: DuckLake has no
  constraints, so the key is guaranteed by the write path.
- `continuo-trino-adapter`: `append` and `upsert` through a stage table
  shaped like the target, then one `INSERT ... SELECT` or one `MERGE` — one
  Iceberg commit per write; the stage is dropped best-effort afterwards.
  `table_columns` reads `information_schema`. `ensure_table` accepts an
  upsert key (no constraint DDL) and rejects it with `format_version: 1`.
  `load` now shares the stage helpers (no behavior change).
- The harness writes each node with its `mode_type`: `load` for `replace`,
  `append`, or `upsert` with the node's `primary_key` (also passed to
  `ensure_table`). An adapter without the needed capability fails with
  `LoadError: <Adapter> does not support <mode>` before the script runs; an
  `upsert` whose rows repeat a key fails with `ConformError` and writes
  nothing. Before an `append` or `upsert` the live table's columns and
  nullability are compared with `output_columns`; any difference is a
  `LoadError` naming each one. See "Write modes" in the README.

### Changed

- The Python version is pinned in one place, `.python-version` (`3.14.2`), the
  single source of truth: the engine images' `Dockerfile`s pin the same exact
  patch (`python:3.14.2-slim`), and `requires-python`, mypy `python_version`,
  the trove classifiers and `setup-python` inputs track its major.minor. A new
  consistency guard (`tests/test_python_version_consistency.py`) scans every
  `pyproject.toml`, `Dockerfile*` and workflow and fails on any drift.
- `continuo-engine-contract` 0.7.3 -> 0.8.0 (the write capabilities above).
  The runtime and the three adapters pin it exactly, as do the engine images'
  `image-requirements-*.txt`.
- **Breaking for newly copied templates.** The `template/` release workflow
  calls continuo's public release API instead of an unauthenticated webhook:
  - `RELEASE_ENDPOINT` keeps its name but is now the origin of continuo's
    `auth.publicUrl` (`scheme://host[:port]`, no path), which is also the OIDC
    audience.
  - The repository must be bound to the service in continuo's
    `ciAuth.bindings`.
  - The first release of a service is an operator bootstrap; the workflow never
    sends `bootstrap`.

  The workflow checks the endpoint and does an authenticated read of
  `/api/v1/current-prod` before building, so a wrong URL, audience or missing
  binding fails early. It then submits to `<RELEASE_ENDPOINT>/api/v1/releases`
  with a fresh GitHub Actions OIDC token per call and polls
  `GET /api/v1/releases/{id}` for up to about 50 minutes (`timeout-minutes: 60`),
  failing on `rejected` and `superseded`. A newer push no longer cancels a run
  that is waiting for its release (`cancel-in-progress: false`). Workflows
  already copied from the template are untouched.

## [0.8.0] - 2026-10-01

Packages in this release: `continuo-python-runtime` 0.8.0 (no runtime code
change; the version carries the new image set), `continuo-duckdb-adapter` 0.1.0
(new), `continuo-engine-contract` 0.7.3, `continuo-postgres-adapter` 0.2.2 and
`continuo-trino-adapter` 0.2.1 (both unchanged). Images:
`continuo-python-runtime-{postgres,trino,duckdb}:v0.8.0`, each linux/amd64 and
linux/arm64.

### Added

- `continuo-duckdb-adapter` 0.1.0: a DuckDB engine adapter on a DuckLake
  (Postgres catalog, Parquet data on S3/MinIO) with the same behaviour as the
  postgres and trino adapters: validation DDL, `check_binds`, and the
  python-node `fetch` / `ensure_table` / `load`. Physical layout `config`
  accepts `partitioned_by` (identity, `bucket`, `year`/`month`/`day`/`hour`)
  and `sorted_by`; any other key is rejected. Configured with `DUCKDB_*`
  environment variables (catalog, data path, S3).
  The catalog password is kept out of the connection string (private libpq
  passfile) and redacted, with the S3 secret, from every engine error; a first
  attach of a fresh catalog from concurrent Jobs is retried; `check_binds` runs
  in a read-only transaction; `DUCKDB_TEMP_DIRECTORY` (set in the image) gives
  DuckDB a writable spill directory; the `aws` extension is baked for the
  credential-chain S3 path.
- `Dockerfile.duckdb` (engine image, DuckDB extensions baked in for offline,
  non-root start), the `tests/smoke/duckdb-stack` compose stack, and CI jobs
  that run the adapter's integration suite and the image smoke test against it.
- Verified releases on amd64 and arm64. A `v*` tag now runs one pipeline
  (`publish-pypi.yml`): the five packages are installed from the built
  `dist/` into a clean venv on native amd64 and arm64 runners, and every engine
  image is built from those same wheels and smoke-tested on both, all BEFORE the
  irreversible PyPI upload. The upload sends those same `dist/` files. Images
  are then built natively per architecture (no QEMU) from the published pins and
  pushed by digest only; each digest is pulled and smoke-tested (including the
  offline DuckDB extension check); only then are the two verified digests given
  the bare `vX.Y.Z` tag. A failed verify or promote fails the workflow, so no
  GitHub Release is created.
- `-test` tags are a real dress rehearsal: TestPyPI upload with a unique
  `<version>.dev<N>` per run, images built from TestPyPI (first-party packages
  only; third-party dependencies still come from PyPI), pushed under a `-test`
  tag. `workflow_dispatch` with `dry_run: true` runs the same graph and
  publishes, pushes and promotes nothing.
- `Dockerfile.*` accept `WHEEL_SOURCE=testpypi`; an unknown `WHEEL_SOURCE` now
  fails the build instead of silently installing from PyPI.
  `image-requirements-*.txt` also pin `continuo-engine-contract`.
- Pull requests that touch images run the package gate and the image smoke tests
  on arm64 as well as amd64, through the same reusable workflows
  (`verify-packages.yml`, `image-build.yml`, `image-smoke.yml`) the release uses.

### Changed

- `images.yml` is the pull-request path only; it no longer builds or pushes on a
  tag. The postgres image smoke runs against `tests/smoke/postgres-stack`
  instead of a job service container, so one workflow serves every engine.
- `release.yml` waits only for `publish-pypi.yml`, which now covers the images,
  and selects that tag's own run (a `-test` rehearsal on the same commit no
  longer shares its lookup).

### Fixed

- `scripts/check_version_bumps.py` no longer treats a `-test` rehearsal tag as
  the previous release. A package changed without a version bump could diff
  clean against a rehearsal tag, pass the guard, and ship old bytes through
  `skip-existing`.
- A tag with a differently-cased `-Test` is now a rehearsal in
  `publish-pypi.yml` as it already was in `release.yml`, never a real release.

## [0.7.0] - 2026-09-29

### Added

- `python-api` node kind: a script node with no declared reads. It may name a
  Kubernetes Secret with `secret_ref` (must match `continuo-api-*`), which
  continuo attaches to the node's pod as env vars. `secret_ref` is rejected on
  every other kind. Existing contracts and their content hashes are unchanged.

## [0.6.0] - 2026-09-28

### Breaking

- The script node kind is named `python-node` (the default when `kind` is
  omitted). A contract declaring `kind: python-model` is rejected at load.
  Re-running `continuo-runtime merge` changes every python node's
  `config_hash`, so each node is re-validated once on its next release.

### Changed

- Contract rules, hash inputs and run producers are looked up per kind from
  registries pinned to `KINDS`.

## [0.5.0] - 2026-09-09

### Added

- `check_binds` validation op: EXPLAINs a dbt test's compiled SQL against the
  candidate schema and creates nothing, so a test that names a dropped or
  renamed column fails. Reads the SQL from `CANDIDATE_SQL_URI`, like
  `build_from_sql`.

### Changed

- `continuo-postgres-adapter` and `continuo-trino-adapter` 0.2.0 → 0.2.1.

## [0.4.1] - 2026-08-25

### Added

- Bring-your-own engine adapters: `continuo-postgres-adapter` and
  `continuo-trino-adapter` are separate installable packages, so an install
  can supply its own engine adapter independently of the runtime.
- `scripts/check_version_bumps.py`: fails a release when a package's source
  changed since the last tag but its version did not.

### Changed

- Deterministic python-node container builds: pinned per-engine
  image-requirements and hashed template Dockerfiles.
- `continuo-engine-contract` 0.7.2 → 0.7.3.

## [0.4.0] - 2026-08-21

### Added

- `python-csv` node kind: a contract-only CSV loader (no user script) that
  materializes the declared table from the contract's CSV URI, with a
  validation header check.

### Changed

- `continuo-engine-contract` 0.7.1 → 0.7.2.

### Fixed

- `release.yml`'s github-release job now checks out the repository; without the
  checkout it could not read the tag's files.

## [0.3.1] - 2026-08-21

### Added

- Apache License 2.0, `CODE_OF_CONDUCT.md`, `DCO` with CI-enforced sign-off,
  `CONTRIBUTING.md`, `SECURITY.md`, and a gitleaks + Trivy security-scanning
  pipeline, ahead of open-sourcing this repository.
- `.github/workflows/release.yml`: creates a GitHub Release for a version tag
  once that tag's PyPI publish and both engine images finish successfully —
  the one place documenting "this tag = these packages + these images",
  since PyPI's release history says nothing about the ghcr.io images.

### Changed

- `continuo-engine-contract` 0.7.0 → 0.7.1: the published wheel now embeds
  `LICENSE`/`NOTICE` and declares license metadata, which the 0.7.0 wheel
  omitted. Every exact pin on it — root `pyproject.toml` and both adapters'
  — updated to match.

## [0.3.0] - 2026-08-20

### Added

- `continuo-engine-contract` is now vendored in this repository as a uv
  workspace member (renamed from `continuo-validation-contract`), replacing
  the external PyPI dependency of the same content.
- Ported the `validation-op` CLI path and its test suite in from
  continuo-validation.

### Removed

- The external `continuo-validation-contract` PyPI dependency, and every
  `continuo_validation_contract` reference across the codebase.

## [0.2.1] - 2026-08-10

### Changed

- Contract pin bumped to 0.6.0; `ensure_table` aligned with the port's
  `config` parameter.

### Fixed

- Swept the remaining `contract==0.4.0` pin sites; added a guard against
  future pin drift.

### Added

- CI publishes the runtime base images for both amd64 and arm64.

## [0.2.0] - 2026-08-08

### Added

- Three-part content hash, replacing the earlier single-hash formula.
- Physical-layout `config` on the node contract (partitioning, sort order,
  format).
- A static in-repo import-closure resolver, and a lint rule rejecting
  dynamic-import constructs.

### Changed

- Adopted continuo-validation-contract 0.4.0's type grammar and read gate.

### Fixed

- Closure resolver correctness and `sys.path` handling: index-name
  uniqueness under truncation, cyclic `config` detection, UTF-8-BOM
  decoding, unconditional `sys.path` repositioning.

## [0.1.0] - 2026-08-03

### Added

- Initial release: the runtime harness (`conform()`, `RunContext`, the
  closure resolver), the `continuo-runtime` CLI, and the Postgres/Trino
  engine adapters.
