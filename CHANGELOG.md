# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

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
