"""Content hashing: three independent parts folded into one content_hash.

A Python node's ``content_hash`` folds three parts: ``source_hash`` (the node's
own script bytes), ``shared_code_hash`` (the import closure, each file with its
repo-relative path) and ``config_hash`` (the node's code projection). The parts
are computed from already-gathered bytes and a parsed node, so this module does
no filesystem access and knows nothing about ``closure.py``.

``shared_code_hash``, ``config_hash`` and the fold are computed by
continuo-contracts, the library continuo's topology-controller pins and
recomputes the config part with. Nothing here canonicalises or normalises: the
library does, so every producer of a hash writes the same bytes. The fold is
the three-part fold that older topology-controllers check on the wire, so an
entry this module writes passes their re-computation unchanged.

The library refuses a few ``config`` values the contract loader accepts (an
integer beyond 2**53 - 1, NaN or an infinity, nesting deeper than 99 levels, a
lone surrogate). Each refusal reaches the caller as a ``ContractError`` naming
the node.
"""

from collections.abc import Mapping
from hashlib import sha256

from continuo_contracts.errors import ContractsError
from continuo_contracts.hashing import fold3, shared_hash
from continuo_contracts.python import PythonColumn, PythonNode, projection_hash

from continuo_python_runtime.contract.model import Node
from continuo_python_runtime.errors import ContractError


def source_hash(primary: bytes) -> str:
    """Bare hex `sha256(primary)` — the node's own script bytes (a csv node's uri)."""
    return sha256(primary).hexdigest()


def to_python_node(node: Node) -> PythonNode:
    """The library's view of *node*: the fields its code projection is made of."""
    return PythonNode(
        schema=node.schema,
        table=node.table,
        kind=node.kind,
        script=node.script,
        reads=dict(node.reads),
        output_columns=tuple(
            PythonColumn(column.name, column.type, column.nullable)
            for column in node.output_columns
        ),
        extra_columns=node.extra_columns,
        config=dict(node.config),
        secret_ref=node.secret_ref,
        mode_type=node.mode_type,
        primary_key=tuple(node.primary_key),
        assets=node.assets,
    )


def config_hash(node: Node) -> str:
    """Bare hex SHA-256 of the RFC 8785 bytes of the node's code projection."""
    try:
        return projection_hash(to_python_node(node))
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
