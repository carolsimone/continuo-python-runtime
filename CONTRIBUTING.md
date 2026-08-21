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
for the Postgres/Trino integration tests).

```bash
uv sync --all-packages --all-groups
```

This is a uv workspace: the root package (`continuo_python_runtime/`, the harness), the
port (`contract/`), and the two engine adapters (`adapters/postgres/`, `adapters/trino/`)
are separate packages sharing one lockfile.

## Before you open a pull request

```bash
uv run ruff check .
uv run ruff check contract
uv run mypy continuo_python_runtime
uv run mypy contract/continuo_engine_contract
uv run --package continuo-python-runtime-postgres mypy adapters/postgres/continuo_python_runtime_postgres
uv run --package continuo-python-runtime-trino mypy adapters/trino/continuo_python_runtime_trino
uv run pytest --cov=continuo_python_runtime -m "not image" -v
uv run pytest contract/tests -v
uv run pytest adapters/postgres/tests adapters/trino/tests -m "not integration" -v
```

These are exactly what `.github/workflows/ci.yml` runs. Integration tests against a real
Postgres/Trino stack need Docker and are not required for most changes — see
`.github/workflows/ci.yml` for how CI stands them up if you want to run them locally.

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

## Code of conduct

Participation in this project is governed by our
[Code of Conduct](CODE_OF_CONDUCT.md).

## Reporting security issues

Please don't open a public issue — see [SECURITY.md](SECURITY.md).
