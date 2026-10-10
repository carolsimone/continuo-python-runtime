# continuo-python-runtime

The validation/execution runtime that continuo's executor runs as a Kubernetes
Job. Python 3.12, uv workspace: the runtime (`continuo_python_runtime`), the
engine contract (`contract/`), and the engine adapters (`adapters/postgres`,
`adapters/trino`, `adapters/duckdb`).

## CHANGELOG is not optional

Every user-facing change keeps `CHANGELOG.md` current. Two steps, both required:

- **On every PR** that changes behavior, the CLI/op surface, a package version,
  or a container: add a bullet under `## [Unreleased]` (Keep a Changelog style —
  Added / Changed / Fixed / Removed). No entry, no merge.
- **On every release**: rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`,
  add a fresh empty `## [Unreleased]` above it, and tag. `release.yml` reads the
  section matching the pushed tag for the GitHub Release notes, so a release with
  no matching section ships with generic auto-notes and the changelog silently
  loses that version.

If the changelog is missing a shipped version, backfill it from that tag's
merged PRs before adding anything new.

## Engine adapter conformance

`contract/continuo_engine_contract/testing/conformance.py`
(`continuo_engine_contract.testing.conformance`) is the shared, published
conformance suite for engine adapter authors. Its dependencies are installed
with `continuo-engine-contract[testing]`.

- Every adapter MUST satisfy the engine contract and pass the shared
  conformance classes for every capability it declares: `IntrospectionConformance`
  for `TableIntrospection`, `AppendConformance` for `AppendCapable`, and
  `UpsertConformance` for `UpsertCapable`. Append and upsert adapters must also
  run the introspection class. An adapter need not declare capabilities its
  engine cannot support.
- Each adapter's integration suite MUST subclass the applicable classes,
  supply connected `adapter` and temporary `schema` fixtures, and run them
  against its real warehouse in CI. In-memory reference tests alone do not
  establish an adapter's conformance. Do not skip or weaken shared checks to
  accommodate an adapter that violates the contract.
- Every new or changed engine-independent contract requirement, including
  bug regressions, MUST have coverage in this shared testing module. Extend
  the applicable conformance class, or add a class for a new capability, so
  all adapters declaring it are checked by the same cases. Exercise the
  reusable cases in `contract/tests/test_conformance_suite.py` as well.
- Engine-specific implementation details, such as SQL generation, driver
  behavior and authentication, belong in the owning adapter's tests. Add a
  shared case when the behavior expresses a requirement of the engine
  contract; unrelated runtime features do not belong in this module.
- Before completing an adapter or contract change, run `contract/tests` and
  the affected adapter's unit and real-engine integration suites in Docker.

## Releasing

A release is one `chore(release):` commit that bumps versions and updates the
changelog, then a `vX.Y.Z` tag on it. Bump the version of every package whose
source changed since the last tag — `scripts/check_version_bumps.py` fails the
PyPI publish otherwise. The tag builds and publishes the `-postgres` / `-trino` / `-duckdb`
images and the PyPI packages. See `CONTRIBUTING.md` for the full steps.

## Conventions

- Standard `logging` for diagnostics, never `print` — except the CLI's
  sentinel-framed result blocks, which own stdout.
- In-repo packages are pinned exactly, not with ranges (see `pyproject.toml`).
- Commits are signed off (`git commit -s`); CI enforces the DCO.
