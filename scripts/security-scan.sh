#!/usr/bin/env bash
# Runs the secret and dependency scanners across the repository.
#
# This is the single primitive shared by CI (.github/workflows/security.yml) and a
# developer running it locally, so both execute byte-identical checks.
#
# Blocking vs advisory:
#   secrets  -> exits non-zero on any finding. A committed secret is never
#               acceptable, so it is high-signal enough to gate a merge.
#   deps     -> reports HIGH/CRITICAL and always exits 0. Base-image and
#               transitive CVEs are frequently unfixable upstream, so gating on
#               them would wedge every pull request behind an ignore-list edit.
#
# Usage:
#   scripts/security-scan.sh                       # every scanner
#   scripts/security-scan.sh secrets                # gitleaks over the working tree
#   scripts/security-scan.sh secrets --history       # gitleaks over full git history
#   scripts/security-scan.sh secrets --range A..B     # gitleaks over commits A..B
#   scripts/security-scan.sh deps                    # trivy dependency CVEs (advisory)
set -uo pipefail

# Single source of truth for the pinned versions. Bump here only.
GITLEAKS_IMAGE="ghcr.io/gitleaks/gitleaks:v8.24.3"
# Pinned at 0.61.1, newer than the version continuo's script uses (0.58.2):
# uv.lock support landed after that release, and an older trivy silently
# reports zero language-specific files scanned rather than failing — a
# false-green dependency gate. Verified 0.61.1 actually parses uv.lock before
# pinning it.
TRIVY_IMAGE="aquasec/trivy:0.61.1"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"

# SARIF output is opt-in via SARIF_DIR (a path relative to the repository root).
# CI sets it so findings can be uploaded to code scanning; local runs leave it
# unset and pay nothing.
SARIF_DIR="${SARIF_DIR:-}"
sarif_out=""
if [ -n "${SARIF_DIR}" ]; then
  sarif_out="${repo_root}/${SARIF_DIR}"
  mkdir -p "${sarif_out}"
fi

# Scanners walk the whole tree, and .worktrees/ holds full stale copies of every
# tracked file. Without this a fixed issue keeps firing from a stale copy.
# Built as an array because trivy takes one --skip-dirs flag per directory; a
# single joined string is silently accepted as one literal path and excludes
# nothing.
TRIVY_SKIP=(
  --skip-dirs .worktrees
  --skip-dirs .claude
  --skip-dirs .venv
)

have_docker() {
  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "error: docker is required for this scanner but is not available" >&2
    return 1
  fi
}

# ── gitleaks ─────────────────────────────────────────────────────────────────
# Three modes:
#   dir            working tree only. Fast, but a secret added in one commit and
#                   removed in a later one is invisible — the working tree never
#                   carried it.
#   --history       walks every commit reachable from HEAD. Catches everything,
#                   but is too slow to be the per-PR gate on a repository this
#                   grows into.
#   --range A..B    walks exactly the commits a pull request introduces. This is
#                   the per-PR gate: it catches an add-then-remove within the same
#                   PR that `dir` would miss, without re-walking history `dir`
#                   already covered on a prior run.
scan_secrets() {
  have_docker || return 1

  local mode="dir" range=""
  case "${1:-}" in
    --history) mode="git" ;;
    --range)   mode="git"; range="${2:?--range requires A..B}" ;;
  esac

  echo "==> gitleaks (${mode}${range:+, ${range}})"

  # The repo is mounted read-only on purpose, so SARIF cannot be written into it.
  # Bind the output directory separately, read-write, at a path of its own.
  local sarif_args=() sarif_mount=()
  if [ -n "${sarif_out}" ]; then
    sarif_mount=(-v "${sarif_out}:/sarif:rw")
    sarif_args=(--report-format sarif --report-path /sarif/gitleaks.sarif)
  fi

  if [ "${mode}" = "dir" ]; then
    docker run --rm \
      -v "${repo_root}:/repo:ro" \
      ${sarif_mount[@]+"${sarif_mount[@]}"} \
      "${GITLEAKS_IMAGE}" \
      dir /repo \
      --config /repo/.gitleaks.toml \
      ${sarif_args[@]+"${sarif_args[@]}"} \
      --redact \
      --no-banner \
      -v
    return
  fi

  # git mode (--history or --range) needs the object database, and in a git
  # worktree .git is a file holding an absolute path to the main repository's
  # gitdir. Bind-mounting the checkout alone leaves that path dangling inside
  # the container, and gitleaks then reports "no leaks found in partial scan" —
  # a scan that examined nothing. Mount both at their real host paths so the
  # absolute reference resolves.
  local git_common mounts=()
  git_common="$(cd "$(git rev-parse --git-common-dir)" && pwd)"
  mounts+=(-v "${repo_root}:${repo_root}:ro")
  case "${git_common}" in
    "${repo_root}"/*) ;;                                   # already covered
    *) mounts+=(-v "${git_common}:${git_common}:ro") ;;
  esac

  local log_opts_args=()
  [ -n "${range}" ] && log_opts_args=(--log-opts="${range}")

  docker run --rm \
    ${mounts[@]+"${mounts[@]}"} \
    ${sarif_mount[@]+"${sarif_mount[@]}"} \
    -w "${repo_root}" \
    "${GITLEAKS_IMAGE}" \
    git "${repo_root}" \
    --config "${repo_root}/.gitleaks.toml" \
    ${log_opts_args[@]+"${log_opts_args[@]}"} \
    ${sarif_args[@]+"${sarif_args[@]}"} \
    --redact \
    --no-banner \
    -v
}

# ── trivy: dependency CVEs ───────────────────────────────────────────────────
# Advisory. Reads uv.lock rather than building anything, so it is fast and needs
# no images present.
scan_deps() {
  have_docker || return 1

  local sarif_args=() sarif_mount=()
  if [ -n "${sarif_out}" ]; then
    sarif_mount=(-v "${sarif_out}:/sarif:rw")
    sarif_args=(--format sarif --output /sarif/trivy-fs.sarif)
  fi

  echo "==> trivy filesystem (dependency CVEs, advisory)"
  docker run --rm \
    -v "${repo_root}:/repo:ro" \
    ${sarif_mount[@]+"${sarif_mount[@]}"} \
    "${TRIVY_IMAGE}" \
    filesystem /repo \
    ${sarif_args[@]+"${sarif_args[@]}"} \
    --scanners vuln \
    --severity HIGH,CRITICAL \
    --ignore-unfixed \
    "${TRIVY_SKIP[@]}" \
    --exit-code 0 \
    --quiet
}

case "${1:-all}" in
  secrets) shift; scan_secrets "$@" ;;
  deps)    scan_deps ;;
  all)
    rc=0
    scan_secrets || rc=1
    scan_deps    || true   # advisory
    exit "${rc}"
    ;;
  *)
    echo "usage: $0 [secrets [--history|--range A..B]|deps|all]" >&2
    exit 2
    ;;
esac
