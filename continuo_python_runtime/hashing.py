"""Content hashing: three independent parts folded into one content_hash.

A Python node's ``content_hash`` folds three parts: ``source_hash`` (the node's
own script bytes, or a csv node's uri), ``shared_code_hash`` (the import closure
and the declared assets, each file with its repo-relative path) and
``config_hash`` (the code projection of the node's contract). The parts are
computed from already-gathered bytes and a loaded node, so this module does no
filesystem access and knows nothing about ``closure.py``.

``config_hash`` is computed from ``Node.python``, the ``PythonNode`` that
continuo-contracts reads from the node's ODCS contract: the runtime keeps no
mapping of its own from a node to the library's view of it. ``shared_code_hash``,
``config_hash`` and the fold are computed by continuo-contracts, the library
continuo's topology-controller pins and recomputes the config part with. Nothing
here canonicalises or normalises: the library does, so every producer of a hash
writes the same bytes, and a node declared as a contract file and the same node
declared as a wire entry have one hash.

The library refuses a few ``config`` values (an integer beyond 2**53 - 1, NaN
or an infinity, nesting deeper than 99 levels, a lone surrogate). No contract
file holds one, since the loader reads each file with the library's strict
YAML reader, which refuses them too; a refusal for a node built in code
reaches the caller as a ``ContractError`` naming the node.
"""

from collections.abc import Mapping
from hashlib import sha256

from continuo_contracts.errors import ContractsError
from continuo_contracts.hashing import fold3, shared_hash
from continuo_contracts.python import projection_hash

from continuo_python_runtime.contract.model import Node
from continuo_python_runtime.errors import ContractError


def source_hash(primary: bytes) -> str:
    """Bare hex `sha256(primary)` — the node's own script bytes (a csv node's uri)."""
    return sha256(primary).hexdigest()


def config_hash(node: Node) -> str:
    """Bare hex SHA-256 of the RFC 8785 bytes of the code projection of ``node.python``."""
    try:
        return projection_hash(node.python)
    except ContractsError as exc:
        raise ContractError(str(exc)) from exc


def shared_code_hash(units: Mapping[str, bytes]) -> str:
    """`""` when there are no units, else bare hex over each unit's path and digest.

    *units* maps a repo-relative POSIX path to the file's bytes.
    """
    try:
        return shared_hash(units)
    except ContractsError as exc:
        raise ContractError(str(exc)) from exc


def content_hash_fold(source: str, shared: str, config: str) -> str:
    """`"sha256:"` plus the hex SHA-256 of the three parts joined by `|`."""
    return fold3(source, shared, config)


def hash_parts(node: Node, primary: bytes, units: Mapping[str, bytes]) -> dict[str, str]:
    """Return all four fields: the three parts plus their fold."""
    source = source_hash(primary)
    shared = shared_code_hash(units)
    config = config_hash(node)
    return {
        "source_hash": source,
        "shared_code_hash": shared,
        "config_hash": config,
        "content_hash": content_hash_fold(source, shared, config),
    }
