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
