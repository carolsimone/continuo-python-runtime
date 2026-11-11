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


def resolve_asset_path(asset: str, repo_root: Path, *, context: str) -> Path:
    """Resolve ``asset`` (relative to ``repo_root``) and validate it.

    Rejects absolute paths, paths that escape ``repo_root`` once symlinks
    resolve, paths that do not exist and paths that are not a regular file.

    Args:
        asset: The asset path as declared in the contract, relative to
            ``repo_root``.
        repo_root: The repository root the asset path is resolved against.
        context: A label prefixed to every error message (e.g. a node's
            relation) to identify which node's asset failed.

    Raises:
        ContractError: If the path is absolute, escapes the repository root,
            does not exist or is not a file.
    """
    if Path(asset).is_absolute():
        raise ContractError(
            f"{context}: asset path {asset!r} must be relative to the repository root"
        )

    asset_path = (repo_root / asset).resolve()
    if not asset_path.is_relative_to(repo_root.resolve()):
        raise ContractError(f"{context}: asset path {asset!r} escapes the repository root")
    if not asset_path.exists():
        raise ContractError(f"{context}: asset not found: {asset}")
    if not asset_path.is_file():
        raise ContractError(f"{context}: asset {asset!r} is not a file")

    return asset_path
