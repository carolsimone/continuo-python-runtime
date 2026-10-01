# Contributing to Continuo Python Runtime

Thanks for your interest. This repo is maintained by one person, so please read this
before opening a large pull request — it will save us both time.

## Before you start

For anything beyond a bug fix or a docs correction, **open an issue first** and describe
what you want to change. This repo has a few conventions that are load-bearing for the
rest of Continuo (the contract's hash and layout config, the closure resolver's import
roots, the sqlglot read gate), so a pull request that cuts across them is painful to land
no matter how good the code is.

## Licensing and sign-off

This project is licensed under the Apache License 2.0. Contributions are accepted under
the same license.

We use the [Developer Certificate of Origin](DCO) — a short statement that you wrote the
code, or otherwise have the right to submit it. You agree to it by adding a sign-off line
to each commit:

```
Signed-off-by: Your Name <your.email@example.com>
```

`git commit -s` adds this for you. To sign off a branch you already wrote:

```bash
git rebase --signoff origin/main
git push --force-with-lease
```

A CI check enforces this on every pull request. There is no separate agreement to sign.

We do **not** use per-file license headers. The root `LICENSE` covers the whole
repository; please do not add headers to new files.

## Development setup

Prerequisites: Python 3.14+, [uv](https://docs.astral.sh/uv/), and Docker (only needed
for the Postgres/Trino/DuckLake integration tests and the csv-reader/validation-runner
integration tests, which start a real minio backend via `docker run`).

```bash
uv sync --all-packages --all-groups
```

This is a uv workspace: the root package (`continuo_python_runtime/`, the harness), the
port (`contract/`), and the three engine adapters (`adapters/postgres/`, `adapters/trino/`, `adapters/duckdb/`)
are separate packages sharing one lockfile.

## Before you open a pull request

```bash
uv run ruff check .
uv run ruff check contract
uv run mypy continuo_python_runtime
uv run mypy contract/continuo_engine_contract
uv run --package continuo-postgres-adapter mypy adapters/postgres/continuo_postgres_adapter
uv run --package continuo-trino-adapter mypy adapters/trino/continuo_trino_adapter
uv run --package continuo-duckdb-adapter mypy adapters/duckdb/continuo_duckdb_adapter
uv run pytest --cov=continuo_python_runtime -m "not image and not integration" -v
uv run pytest tests -m integration -v
uv run pytest contract/tests -v
uv run pytest adapters/postgres/tests adapters/trino/tests -m "not integration" -v
uv run pytest adapters/duckdb/tests -m "not integration" -v
```

These are exactly what `.github/workflows/ci.yml` runs. Integration tests against a real
Postgres/Trino/DuckLake stack, or against the csv-reader/validation-runner minio backend, need
Docker and are not required for most changes — see `.github/workflows/ci.yml` for how CI
stands them up if you want to run them locally. The duckdb suite, for example, runs
against `tests/smoke/duckdb-stack/docker-compose.yml`:
`docker compose -f tests/smoke/duckdb-stack/docker-compose.yml up -d --wait`, then
`uv run pytest adapters/duckdb/tests -m integration -v`, then the same compose file with
`down -v`.

Also run the security scan before opening a pull request that touches dependencies or
anything that could carry a credential:

```bash
scripts/security-scan.sh
```

## Conventions

- **Changelog.** A pull request whose changes are worth a release note adds an entry
  under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md), Keep a Changelog style. At
  release time that section is renamed to the new version and a fresh empty
  `## [Unreleased]` goes above it — `.github/workflows/release.yml` reads the section
  matching the pushed tag to build the GitHub Release notes, and falls back to
  GitHub's generated notes if none exists.
- **Python logging.** Use the standard `logging` module for diagnostic output, never
  `print`. The only exception is machine-parsed stdout protocols (e.g. the CLI's
  sentinel-framed result blocks) — those stay as explicit `print`, since stdout is
  reserved exclusively for them.
- **Exact-pinned dependencies.** `continuo-engine-contract` and other in-repo packages
  are pinned exactly, not with a range — see the comment in `pyproject.toml` for why.

## Releasing

A release is one `chore(release):` commit that bumps the version of every package whose
source changed since the last tag (`scripts/check_version_bumps.py` refuses the tag
otherwise) and turns `## [Unreleased]` in `CHANGELOG.md` into `## [X.Y.Z] - date`, then a
`vX.Y.Z` tag on that commit. Pushing the tag starts **one** pipeline,
`.github/workflows/publish-pypi.yml`; read its header comment for the design.

The principle is *build once, test the exact artifact, promote the same bytes*:

1. **Before anything irreversible.** The five packages are built once into `dist/`. On
   native amd64 and arm64 runners they are installed from `dist/` into a clean venv
   (entry point, CLI, `required_env()`), and each engine image is built from those same
   wheels and smoke-tested against a real warehouse.
2. **Upload.** The same `dist/` files go to PyPI. PyPI is immutable from here on.
3. **Images.** Each engine image is built on a native runner per architecture from the
   published pins and pushed to ghcr **by digest only** (no tag). Every digest is pulled
   and run through the same smoke test, including the offline DuckDB extension check.
4. **Promote.** Only when all six cells (3 engines x 2 arches) pass are the two verified
   digests given the bare `vX.Y.Z` tag, with no rebuild. Consumers pin
   `<name>:vX.Y.Z@sha256:<digest>`, so that tag exists only for verified images.
5. `release.yml` creates the GitHub Release only if the whole pipeline succeeded.

### Rehearse first with a `-test` tag

A `-test` tag runs every step above against TestPyPI, so do it on the release commit
before the real tag (put it on that very commit: `check_version_bumps.py` treats an
earlier `-test` tag as the previous release):

```bash
git tag v0.8.0-test1 <release-commit> && git push origin v0.8.0-test1
```

Every package is rewritten to `<version>.dev<N>` (unique per run, TestPyPI is immutable),
the images are built from TestPyPI and tagged `v0.8.0-test1`, and no bare or latest-like
tag is written. Third-party dependencies still come from PyPI; only the five first-party
packages are ever installed from TestPyPI, because TestPyPI hosts junk copies of real
names. Use a new `-testN` for each rehearsal; delete the `-test` image tags in the ghcr
package pages when done.

To exercise the whole graph without any tag (builds and verifies everything on both
arches, publishes, pushes and promotes nothing), from any branch:

```bash
gh workflow run publish-pypi.yml --ref <branch> -f dry_run=true
```

### When a run fails

| Fails at | PyPI | ghcr | Release | Recover |
|---|---|---|---|---|
| `prepare`, `verify-packages`, `preflight-*` | nothing | nothing | none | Fix, delete the tag (`git push --delete origin vX.Y.Z`), re-tag. |
| `publish-packages` | maybe some of the five files | nothing | none | Re-run failed jobs (`skip-existing` makes it resumable), or fix forward. |
| `candidate-build` | published | maybe untagged digests | none | Transient (index lag, runner): re-run failed jobs. Real defect: yank the files on pypi.org and **fix forward** with a new version. |
| `candidate-verify` | published | untagged candidates | none | Same: an image defect means yank and fix forward. |
| `promote` | published | verified digests, maybe some engines tagged | none | Re-run failed jobs; tagging is idempotent and refuses to move an existing tag to different content. |

PyPI files cannot be replaced, only yanked. Untagged candidate digests on ghcr are
garbage that nothing consumes.

### New package names: trusted publishers

A new package needs a PyPI *and* a TestPyPI pending trusted publisher registered
**before** its first tag: workflow file `publish-pypi.yml` and the GitHub environments
`pypi` / `testpypi`. The upload is one call, so a project with no publisher fails it for
the whole tag. This is done by hand on pypi.org / test.pypi.org; nothing in this
repository can do it. A `-test` rehearsal needs all five projects registered on
test.pypi.org, which is also the cheapest way to find a missing one before a real
release. (`dry-run`, used by manual dry runs, is an unprotected environment with no
publisher.) A first-ever image name on ghcr is created private: make it public and link
it to this repository once.

## Code of conduct

Participation in this project is governed by our
[Code of Conduct](CODE_OF_CONDUCT.md).

## Reporting security issues

Please don't open a public issue — see [SECURITY.md](SECURITY.md).
