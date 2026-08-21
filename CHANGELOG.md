# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
