"""Contract v1 loader and validator.

Parses YAML contract files into validated :class:`~continuo_python_runtime
.contract.model.Node` instances, raising :class:`ContractError` for any
structural or semantic violation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from continuo_python_runtime.contract.model import (
    CRITICALITIES,
    EXTRA_COLUMNS_POLICIES,
    Column,
    Node,
)
from continuo_python_runtime.errors import ContractError

_ALLOWED_KEYS = {
    "schema",
    "table",
    "description",
    "owner",
    "schedule",
    "criticality",
    "script",
    "extra_columns",
    "reads",
    "output_columns",
    "content_hash",
}

_REQUIRED_STRING_FIELDS = ("schema", "table", "owner", "schedule", "script")

# NOTE: once PR-4 (Task 7) lands, swap this regex check for a call to
# `continuo_python_runtime.contract.types.parse_sql_type`.
_TYPE_RE = re.compile(
    r"^(BIGINT|INT|INTEGER|DOUBLE PRECISION|TEXT|TIMESTAMP|DATE|BOOLEAN"
    r"|(NUMERIC|DECIMAL)\(\d+,\s*\d+\)"
    r"|(VARCHAR|CHAR)\(\d+\))$",
    re.IGNORECASE,
)


def _node_label(raw: dict[str, Any], source: str) -> str:
    """Build a `source (schema.table)`-style label for error messages."""
    schema = raw.get("schema")
    table = raw.get("table")
    if isinstance(schema, str) and schema and isinstance(table, str) and table:
        return f"{source} ({schema}.{table})"
    return source


def parse_node(raw: dict[str, Any], source: str) -> Node:
    """Validate a single raw mapping and build a :class:`Node`.

    ``source`` is the originating filename; it appears in every error message.
    """
    if not isinstance(raw, dict):
        raise ContractError(f"{source}: node must be a mapping, got {type(raw).__name__}")

    label = _node_label(raw, source)

    unknown = set(raw) - _ALLOWED_KEYS
    if unknown:
        raise ContractError(f"{label}: unknown key(s) {sorted(unknown)}")

    for field in _REQUIRED_STRING_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ContractError(f"{label}: required field '{field}' must be a non-empty string")

    schema = raw["schema"]
    table = raw["table"]
    owner = raw["owner"]
    schedule = raw["schedule"]
    script = raw["script"]

    criticality = raw.get("criticality")
    if not isinstance(criticality, str) or criticality not in CRITICALITIES:
        raise ContractError(
            f"{label}: 'criticality' must be one of {sorted(CRITICALITIES)}, got {criticality!r}"
        )

    extra_columns = raw.get("extra_columns", "raise")
    if extra_columns not in EXTRA_COLUMNS_POLICIES:
        raise ContractError(
            f"{label}: 'extra_columns' must be one of {sorted(EXTRA_COLUMNS_POLICIES)}, "
            f"got {extra_columns!r}"
        )

    reads = raw.get("reads")
    if not isinstance(reads, dict) or not reads:
        raise ContractError(f"{label}: 'reads' must be a non-empty mapping of name -> SQL")
    for name, sql in reads.items():
        if not isinstance(sql, str) or not sql.strip():
            raise ContractError(f"{label}: 'reads.{name}' must be a non-empty SQL string")

    raw_columns = raw.get("output_columns")
    if not isinstance(raw_columns, list) or not raw_columns:
        raise ContractError(f"{label}: 'output_columns' must be a non-empty list")

    seen_names: set[str] = set()
    columns: list[Column] = []
    for entry in raw_columns:
        if not isinstance(entry, dict):
            raise ContractError(
                f"{label}: each 'output_columns' entry must be a mapping, got {entry!r}"
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ContractError(f"{label}: output column missing non-empty 'name'")
        col_type = entry.get("type")
        if not isinstance(col_type, str) or not _TYPE_RE.match(col_type.strip()):
            raise ContractError(
                f"{label}: output column '{name}' has unsupported 'type' {col_type!r}"
            )
        nullable = entry.get("nullable", True)
        if not isinstance(nullable, bool):
            raise ContractError(
                f"{label}: output column '{name}' has non-boolean 'nullable' {nullable!r}"
            )
        if name in seen_names:
            raise ContractError(f"{label}: duplicate column '{name}' in output_columns")
        seen_names.add(name)
        columns.append(Column(name=name, type=col_type, nullable=nullable))

    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ContractError(f"{label}: 'description' must be a string")

    content_hash = raw.get("content_hash")
    if content_hash is not None and not isinstance(content_hash, str):
        raise ContractError(f"{label}: 'content_hash' must be a string")

    return Node(
        schema=schema,
        table=table,
        owner=owner,
        schedule=schedule,
        criticality=criticality,
        script=script,
        reads=dict(reads),
        output_columns=tuple(columns),
        description=description,
        extra_columns=extra_columns,
        content_hash=content_hash,
    )


def load_contract_dir(path: Path) -> list[Node]:
    """Load and validate every `*.yml`/`*.yaml` contract file under ``path``.

    Raises `ContractError` if no nodes are found, if any file's document is
    malformed, or if two nodes across files share the same `(schema, table)`.
    """
    files = sorted(path.glob("*.yml")) + sorted(path.glob("*.yaml"))

    nodes: list[Node] = []
    relation_sources: dict[str, str] = {}

    for file in files:
        text = file.read_text()
        document = yaml.safe_load(text)
        if document is None:
            document = {}
        if not isinstance(document, dict):
            raise ContractError(
                f"{file.name}: contract document must be a mapping, "
                f"got {type(document).__name__}"
            )
        raw_nodes = document.get("nodes")
        if not isinstance(raw_nodes, list):
            raise ContractError(f"{file.name}: 'nodes' must be a list")

        for raw_node in raw_nodes:
            node = parse_node(raw_node, file.name)
            existing_source = relation_sources.get(node.relation)
            if existing_source is not None:
                raise ContractError(
                    f"duplicate node {node.relation} (in {existing_source} and {file.name})"
                )
            relation_sources[node.relation] = file.name
            nodes.append(node)

    if not nodes:
        raise ContractError(f"no contract files found in {path}")

    return nodes
