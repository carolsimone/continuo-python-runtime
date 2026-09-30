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
