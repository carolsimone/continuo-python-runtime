"""Shared script-path resolution used by both the merger and the harness."""

from __future__ import annotations

from pathlib import Path

from continuo_python_runtime.errors import ContractError


def resolve_script_path(script: str, repo_root: Path, *, context: str) -> Path:
    """Resolve ``script`` (relative to ``repo_root``) and validate it.

    Rejects absolute paths, paths that escape ``repo_root`` once resolved,
    and paths that don't point to an existing file.

    Args:
        script: The script path as declared in the contract, relative to
            ``repo_root``.
        repo_root: The repository root the script path is resolved against.
        context: A label prefixed to every error message (e.g. a node's
            relation or a node id) to identify which node's script failed.

    Raises:
        ContractError: If the path is absolute, escapes the repository
            root, or does not resolve to an existing file.
    """
    if Path(script).is_absolute():
        raise ContractError(
            f"{context}: script path {script!r} must be relative to the repository root"
        )

    script_path = (repo_root / script).resolve()
    if not script_path.is_relative_to(repo_root.resolve()):
        raise ContractError(f"{context}: script path {script!r} escapes the repository root")
    if not script_path.is_file():
        raise ContractError(f"{context}: script not found: {script}")

    return script_path
