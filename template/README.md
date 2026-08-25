# Continuo Python Domain Repository Template

This is a copy-ready template for implementing a [Continuo Python domain repo](https://github.com/carolsimone/continuo-python-runtime).

## Quick Start

1. **Copy this directory** to a new repository
2. **Rename the service**: Edit `.github/workflows/release.yml` and update the `SERVICE` environment variable to your service name
3. **Configure repository variables** in GitHub (Settings → Secrets and variables → Actions):
   - `REGISTRY`: Your Docker registry (e.g., `ghcr.io/org`)
   - `BUCKET`: Your S3 bucket for contract artifacts
   - `RELEASE_ENDPOINT`: Your release webhook endpoint. This is the **base
     URL** of the Continuo API (no `/releases` suffix) — the workflow
     appends `/releases` itself.
4. **Configure repository secrets**:
   - `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` for S3 uploads
   - `release.yml` already logs in to `ghcr.io` with the built-in `GITHUB_TOKEN`
     (no extra secret needed) — only add your own login step if `REGISTRY`
     points at a registry other than `ghcr.io`
5. **Write your contracts** in `contracts/` and **implement scripts** in `scripts/`
6. **Push to main** to trigger the release pipeline

## Choosing a base

This template ships two Dockerfiles that produce the same kind of image
(`ENTRYPOINT ["continuo-runtime"]`, `CMD ["run"]`, contracts + scripts baked
in, running as uid 65532) via two different build shapes. Pick one; you only
need one Dockerfile in your repo.

**Shape 1 — `Dockerfile`, `FROM` the engine image (simplest).** Builds
`FROM ghcr.io/carolsimone/continuo-python-runtime-<engine>:vX.Y.Z`, an image
that already has the Continuo runtime and one engine adapter installed and
pinned by the publisher. You only add your `contracts/` and `scripts/` (and
any extra dependency your script needs). Pin by tag or digest
(`:vX.Y.Z@sha256:<digest>`) for reproducibility. Use this unless you have a
specific reason not to.

**Shape 2 — `Dockerfile.pip`, your own base (hash-locked).** Builds
`FROM python:3.14-slim` (or another base you control) and installs
`continuo-python-runtime` plus one `continuo-<engine>-adapter` from PyPI via
`pip install --require-hashes -r requirements.lock`. Use this when you must
control the base image yourself — e.g. your org mandates a specific base,
you need OS packages the engine image doesn't carry, or you're building on a
platform the published engine images don't target. `requirements.lock` is
the one place in this template where `--require-hashes` and a committed
hash-lock are used (the engine images themselves pin by version only); this
is what makes the Shape-2 build deterministic without depending on the
publisher's image layers.

Regenerate `requirements.lock` for your engine and versions with:

```bash
uv pip compile --generate-hashes --python-version 3.14 - -o requirements.lock <<'EOF'
continuo-python-runtime==0.4.0
continuo-<engine>-adapter==X.Y.Z
EOF
```

The committed `requirements.lock` in this template is a **placeholder**: the
adapters were not yet published to PyPI when it was written, so it has no
`--hash` entries and `pip install --require-hashes` will refuse to install it
as-is. Regenerate it once your chosen adapter version is actually on PyPI.

The image name (`continuo-python-runtime-<engine>`, what Shape 1 pulls) and
the pip distribution name (`continuo-<engine>-adapter`, what Shape 2
installs) are two artifacts published from the same adapter source — same
engine, same version, same runtime behavior, different packaging.

## Pipeline Overview

The CI/CD pipeline (`release.yml`) performs the six-step orchestration:
1. **Lint** scripts for hand-written SQL (forbidden)
2. **Validate** contracts against the schema
3. **Run domain tests** (optional, if `tests/` exists)
4. **Merge** contracts into a single artifact
5. **Build and push** Docker image
6. **Upload contract** and **POST release notification**

## Resources

- [Continuo Python Runtime Documentation](https://github.com/carolsimone/continuo-python-runtime)
- [Boundary Contract (design §13)](https://github.com/carolsimone/continuo-python-runtime/blob/main/docs/boundary-contract.md)
