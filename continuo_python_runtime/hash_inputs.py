"""The bytes each node kind hashes, gathered from the repository.

A script kind hashes its script, its in-repo import closure and the assets it
declares (a file that is both is one unit). A csv node has
no script and no closure: its source is its uri, so new file content at the same
uri is new data, not a new node version.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from continuo_python_runtime.closure import resolve_closure
from continuo_python_runtime.contract.model import Node
from continuo_python_runtime.contract.paths import resolve_asset_path, resolve_script_path


@dataclass(frozen=True)
class HashInputs:
    """What a node's hash parts are computed from.

    ``primary`` is the script's bytes (a csv node's uri, encoded). ``units`` maps
    each closure file's and each declared asset's repo-relative POSIX path to
    its bytes. ``code_files`` holds every file that executes for the node, the
    script first, for the lint.
    """

    primary: bytes
    units: dict[str, bytes]
    code_files: tuple[tuple[Path, bytes], ...]


def _script_inputs(node: Node, repo_root: Path) -> HashInputs:
    script_path = resolve_script_path(node.script, repo_root, context=node.relation)
    script_bytes = script_path.read_bytes()
    root = repo_root.resolve()
    members = [(member, member.read_bytes()) for member in resolve_closure(script_path, repo_root)]
    units = {member.relative_to(root).as_posix(): data for member, data in members}
    # Assets are data the script reads, not code that runs, so they are hashed
    # but not part of code_files (which the lint reads).
    for asset in node.assets:
        units[asset] = resolve_asset_path(asset, repo_root, context=node.relation).read_bytes()
    return HashInputs(
        primary=script_bytes,
        units=units,
        code_files=((script_path, script_bytes), *members),
    )


def _csv_inputs(node: Node, repo_root: Path) -> HashInputs:
    return HashInputs(node.reads["csv"].encode(), {}, ())


HASH_INPUTS: dict[str, Callable[[Node, Path], HashInputs]] = {
    "python-node": _script_inputs,
    "python-csv": _csv_inputs,
    "python-api": _script_inputs,
}


def gather_hash_inputs(node: Node, repo_root: Path) -> HashInputs:
    """Read the files *node* hashes, by its kind."""
    return HASH_INPUTS[node.kind](node, repo_root)
