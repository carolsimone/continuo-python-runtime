"""The contract loader: one ODCS contract per node.

A contracts folder holds one ODCS v3.2.0 contract per node, in a file named
``<table>.odcs.yaml``, or ``<schema>.<table>.odcs.yaml`` when two nodes of one
service share a table name. continuo-contracts reads each file strictly, checks
it and maps it to its ``PythonNode``; the runtime then applies the rules it
keeps for itself and builds its :class:`~continuo_python_runtime.contract.model
.Node`. Every refusal is a :class:`ContractError` that names the file.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from continuo_contracts import yaml_strict
from continuo_contracts.errors import StrictYamlError
from continuo_contracts.python import PythonNode, node_from_odcs
from continuo_contracts.validate import validate_python_contract
from continuo_contracts.vocabulary import continuo_values
from sqlglot import Dialect

from continuo_python_runtime.contract.kinds import rules_for
from continuo_python_runtime.contract.model import Column, Node
from continuo_python_runtime.errors import ContractError

CONTRACT_SUFFIX = ".odcs.yaml"
# The endings of a YAML file's name, compared in lower case: any file that ends
# in one of them and not exactly in CONTRACT_SUFFIX is refused.
_YAML_SUFFIXES = (".yml", ".yaml")

_JSON_SCALARS = (str, int, float, bool, type(None))

# What a document that is not a mapping is, as the words of a message.
_DOCUMENT_KINDS = {
    list: "a list",
    str: "a string",
    bool: "a boolean",
    int: "a number",
    float: "a number",
}


def _validate_config(raw: Any, label: str) -> dict[str, Any]:
    """Validate the node's physical-layout ``config`` as a JSON-shaped mapping.

    The engine's adapter, not this loader, owns the vocabulary, so the only
    rules here are the ones the hash and the adapters need: it is a mapping,
    every key at every level is a string, and every value is a JSON scalar, a
    list or a mapping. A contract file always passes, since ``yaml_strict``
    reads nothing else; a document built in code can hold a key that is not a
    string, a value JSON cannot write or a container that contains itself, and
    each is refused here, naming the node.

    ``active`` holds the ``id()`` of every dict or list being descended into:
    a container is added just before its items are checked and discarded right
    after, so meeting one that is still in ``active`` is a cycle, while one
    container reached twice at sibling positions is not.
    """
    if not isinstance(raw, dict):
        raise ContractError(f"{label}: 'config' must be a mapping")

    active: set[int] = set()

    def _check(value: Any, key: Any) -> None:
        """Validate ``value`` (found under ``key`` in its enclosing mapping)."""
        if isinstance(value, (dict, list)):
            container_id = id(value)
            if container_id in active:
                raise ContractError(
                    f"{label}: 'config' contains a circular reference at {key!r}"
                )
            active.add(container_id)
            try:
                if isinstance(value, dict):
                    for sub_key, sub_value in value.items():
                        if not isinstance(sub_key, str):
                            raise ContractError(
                                f"{label}: 'config' keys must be strings, got {sub_key!r}"
                            )
                        _check(sub_value, sub_key)
                else:
                    for item in value:
                        _check(item, key)
            finally:
                active.discard(container_id)
        elif not isinstance(value, _JSON_SCALARS):
            raise ContractError(
                f"{label}: 'config' value for {key!r} is not JSON-serializable: {value!r}"
            )

    _check(raw, None)
    return raw


def _upsert_key(python: PythonNode, label: str) -> tuple[str, ...]:
    """Return the key the harness upserts on: the node's key when it upserts, else none.

    On a replace or append node a key only describes the table. The names of
    an upsert key are compared case-insensitively too, so a key cannot name
    two columns that differ only in case.
    """
    if python.mode_type != "upsert":
        return ()
    seen: set[str] = set()
    for name in python.primary_key:
        folded = name.lower()
        if folded in seen:
            raise ContractError(
                f"{label}: the upsert key names {name!r} more than once "
                "(names are compared case-insensitively)"
            )
        seen.add(folded)
    return tuple(python.primary_key)


def node_from_contract(
    doc: Mapping[str, Any], file: Path, *, dialect: str | None, check_reads: bool
) -> Node:
    """Check the ODCS contract ``doc``, read from ``file``, and build its :class:`Node`.

    continuo-contracts checks the contract first
    (``validate_python_contract``, with no service, of which only the base
    name of ``file`` counts for the file-name rule). When it finds issues they
    become one :class:`ContractError` with one line per issue,
    ``f"{file}: {issue}"``, and nothing else runs.

    The runtime's own rules follow, each naming ``file`` and the relation:
    the kind's reads rule (:mod:`~continuo_python_runtime.contract.kinds`),
    which parses each read of a python-node as one read query under
    ``dialect`` unless ``check_reads`` is false; the shape of ``config``; and
    the case-insensitive uniqueness of an upsert key's names.

    The node takes ``owner`` from ``team.name``, ``description`` from
    ``description.purpose`` (``""`` when absent), and ``schedule``,
    ``criticality`` and ``timeout_minutes`` (``None`` when absent) from the
    contract's ``continuo`` custom properties. Every other field is the
    library's ``PythonNode``, kept as ``python``: the kind as the library
    reads it, the script, the reads, the columns (nullable unless
    ``required``), ``extra_columns``, ``config``, ``secret_ref``,
    ``mode_type``, the key when the node upserts and the assets.
    """
    issues = validate_python_contract(doc, path=str(file), service=None)
    if issues:
        raise ContractError("\n".join(f"{file}: {issue}" for issue in issues))
    python = node_from_odcs(doc)
    label = f"{file} ({python.relation})"

    reads = rules_for(python.kind, label).reads.validate(
        dict(python.reads), label, dialect=dialect, check_reads=check_reads
    )
    config = _validate_config(python.config, label)
    primary_key = _upsert_key(python, label)

    values = continuo_values(doc.get("customProperties"))
    description = doc.get("description", {}).get("purpose", "")
    return Node(
        schema=python.schema,
        table=python.table,
        owner=doc["team"]["name"],
        schedule=values["schedule"],
        criticality=values["criticality"],
        script=python.script,
        reads=reads,
        output_columns=tuple(
            Column(name=column.name, type=column.type, nullable=column.nullable)
            for column in python.output_columns
        ),
        python=python,
        path=file,
        description=description,
        extra_columns=python.extra_columns,
        config=config,
        kind=python.kind,
        secret_ref=python.secret_ref,
        mode_type=python.mode_type,
        primary_key=primary_key,
        timeout_minutes=values.get("timeoutMinutes"),
        assets=tuple(python.assets),
    )


def _read_contract(file: Path) -> Mapping[str, Any]:
    """Return the document of a contract file, read strictly by continuo-contracts."""
    try:
        text = file.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"{file}: the file is not UTF-8 text ({exc})") from None
    try:
        doc = yaml_strict.load(text)
    except StrictYamlError as exc:
        raise ContractError(f"{file}: {exc}") from None
    if not isinstance(doc, Mapping):
        holds = "nothing" if doc is None else _DOCUMENT_KINDS.get(type(doc), type(doc).__name__)
        raise ContractError(
            f"{file}: a contract file holds one ODCS contract, a mapping; this one holds {holds}"
        )
    return doc


def load_contract_dir(
    path: Path, *, dialect: str | None = None, check_reads: bool = True
) -> list[Node]:
    """Load and check every ``*.odcs.yaml`` contract in ``path``, in file-name order.

    Any other file whose name ends in ``.yml`` or ``.yaml``, in any case
    (``legacy.YML``, ``t.odcs.YAML``, ``t.odcs.yml``), is refused: one
    :class:`ContractError` names every such file, one line each, with how to
    convert it. An image pairs a runtime with its contracts, so a contract in
    another format is a mistake, never a node to skip. Files with any other
    extension are not read.

    ``check_reads=False`` skips only the check that each read of a python-node
    parses as one read query; every other rule of :func:`node_from_contract`
    and every rule here (the dialect name, the duplicate relations, the empty
    folder) still runs. The harness
    (:func:`~continuo_python_runtime.harness.run_node`) passes it, because
    parsing the reads again at container start can only disagree with the
    checks already passed, never catch a new problem:

    - CI already checked the reads (``continuo-runtime validate``) under the
      repository's ``--dialect``, and continuo checked them again with its own
      parser before promoting the release.
    - The harness has no ``--dialect`` of its own, so it would parse with
      sqlglot's dialect-neutral grammar, which refuses ordinary postgres such
      as ``a ~ 'x'`` that parses under ``--dialect postgres``.
    - Nothing at run time reads the parse: ``ctx.read`` finds a read by name
      and hands its SQL to the adapter as written.

    ``dialect`` is checked once, before any file is read: sqlglot raises the
    same ``ValueError`` for an unknown dialect name as for a read it cannot
    parse, so a typo such as ``--dialect POSTGRES`` is reported as a bad flag
    instead of blaming a valid read.

    Raises ``ContractError`` for an unknown ``dialect``, a file in another
    format, a folder with no contract, a contract that is refused, and two
    files that declare the same ``<schema>.<table>``.
    """
    if dialect is not None:
        try:
            Dialect.get_or_raise(dialect)
        except ValueError as exc:
            raise ContractError(f"unknown --dialect {dialect!r}: {exc}") from exc

    entries = sorted(path.iterdir()) if path.is_dir() else []
    yaml_files = [
        entry for entry in entries if entry.name.lower().endswith(_YAML_SUFFIXES)
    ]
    files = [entry for entry in yaml_files if entry.name.endswith(CONTRACT_SUFFIX)]
    others = [entry for entry in yaml_files if not entry.name.endswith(CONTRACT_SUFFIX)]
    if others:
        raise ContractError(
            "\n".join(
                f"{other}: runtime 0.13.0 reads only ODCS contracts named "
                "<table>.odcs.yaml; convert this file (docs/boundary-contract.md)"
                for other in others
            )
        )
    if not files:
        raise ContractError(f"no contract files found in {path}")

    nodes: list[Node] = []
    relation_files: dict[str, Path] = {}
    for file in files:
        node = node_from_contract(
            _read_contract(file), file, dialect=dialect, check_reads=check_reads
        )
        existing = relation_files.get(node.relation)
        if existing is not None:
            raise ContractError(
                f"duplicate node {node.relation} (in {existing} and {file})"
            )
        relation_files[node.relation] = file
        nodes.append(node)
    return nodes
