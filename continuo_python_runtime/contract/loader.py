"""Contract v1 loader and validator.

Parses YAML contract files into validated :class:`~continuo_python_runtime
.contract.model.Node` instances, raising :class:`ContractError` for any
structural or semantic violation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from continuo_engine_contract.sql import ensure_single_read  # type: ignore[import-untyped]
from sqlglot import Dialect
from sqlglot.errors import TokenError

from continuo_python_runtime.contract.model import (
    CRITICALITIES,
    EXTRA_COLUMNS_POLICIES,
    KINDS,
    Column,
    Node,
)
from continuo_python_runtime.csv_source import parse_csv_uri
from continuo_python_runtime.errors import ContractError
from continuo_python_runtime.types import parse_sql_type

_ALLOWED_KEYS = {
    "schema",
    "table",
    "description",
    "owner",
    "schedule",
    "criticality",
    "kind",
    "script",
    "extra_columns",
    "reads",
    "output_columns",
    "config",
    "content_hash",
}

_REQUIRED_STRING_FIELDS = ("schema", "table", "owner", "schedule")

_ALLOWED_OUTPUT_COLUMN_KEYS = {"name", "type", "nullable"}


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of keeping the last."""


def _strict_construct_mapping(
    loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False
):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise ContractError(f"duplicate key {key!r} at {key_node.start_mark}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _strict_construct_mapping
)


def _node_label(raw: dict[str, Any], source: str) -> str:
    """Build a `source (schema.table)`-style label for error messages."""
    schema = raw.get("schema")
    table = raw.get("table")
    if isinstance(schema, str) and schema and isinstance(table, str) and table:
        return f"{source} ({schema}.{table})"
    return source


_JSON_SCALARS = (str, int, float, bool, type(None))


def _validate_config(raw: Any, label: str) -> dict[str, Any]:
    """Validate the node's physical-layout `config` as a JSON-shaped mapping.

    The engine's adapter — not this loader — owns the vocabulary (§3.3), so the
    only rules here are the ones the hash and the wire format need: it is a
    mapping, every key at every level is a string, and every value is
    JSON-serializable. Non-string keys would make `json.dumps(..., sort_keys=True)`
    raise inside the hasher, and a non-serializable value would break the wire
    artifact — both must surface here, naming the node, not deep in CI.

    YAML aliases (``&anchor`` / ``*anchor``) can make PyYAML construct a
    genuinely cyclic object graph — a mapping whose own value (however deeply
    nested) is itself. Recursing into that without tracking what is already
    on the call stack raises an uncaught ``RecursionError`` instead of this
    function's promised ``ContractError``. ``active`` holds the ``id()`` of
    every dict/list container currently being descended into; a container is
    added just before recursing into its items and discarded right after, so
    re-entering one still on the stack is a genuine back-edge (a cycle), while
    the *same* container reached twice at sibling positions — a legal DAG,
    e.g. one list aliased under two different keys — is not: its first visit
    finishes (and is discarded) before the second begins.
    """
    if raw is None:
        return {}
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


def parse_node(
    raw: dict[str, Any],
    source: str,
    *,
    dialect: str | None = None,
    check_reads: bool = True,
) -> Node:
    """Validate a single raw mapping and build a :class:`Node`.

    ``source`` is the originating filename; it appears in every error message.
    ``dialect`` is a sqlglot dialect name (e.g. ``"postgres"``, ``"trino"``)
    each declared read is checked against via
    :func:`~continuo_engine_contract.sql.ensure_single_read`; ``None``
    (the default) uses sqlglot's dialect-neutral parser.

    ``check_reads=False`` skips *only* that :func:`ensure_single_read` call —
    the read-shape gate — leaving every other rule here (required fields,
    criticality, the ``reads`` map's own shape, output-column types and
    uniqueness, ``config``, ``content_hash``) in force. It exists for
    :func:`~continuo_python_runtime.harness.run_node`; see
    :func:`load_contract_dir` for why the runtime opts out.
    """
    if not isinstance(raw, dict):
        raise ContractError(
            f"{source}: node must be a mapping, got {type(raw).__name__}"
        )

    label = _node_label(raw, source)

    unknown = set(raw) - _ALLOWED_KEYS
    if unknown:
        raise ContractError(f"{label}: unknown key(s) {sorted(unknown)}")

    kind = raw.get("kind", "python-model")
    if kind not in KINDS:
        raise ContractError(
            f"{label}: 'kind' must be one of {sorted(KINDS)}, got {kind!r}"
        )

    for field in _REQUIRED_STRING_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ContractError(
                f"{label}: required field '{field}' must be a non-empty string"
            )

    schema = raw["schema"]
    table = raw["table"]
    owner = raw["owner"]
    schedule = raw["schedule"]

    if kind == "python-csv":
        if "script" in raw:
            raise ContractError(
                f"{label}: 'script' is forbidden for kind python-csv "
                "(csv nodes are contract-only)"
            )
        script = ""
    else:
        script = raw.get("script")
        if not isinstance(script, str) or not script.strip():
            raise ContractError(
                f"{label}: required field 'script' must be a non-empty string"
            )

    criticality = raw.get("criticality")
    if not isinstance(criticality, str) or criticality not in CRITICALITIES:
        raise ContractError(
            f"{label}: 'criticality' must be one of {sorted(CRITICALITIES)}, got {criticality!r}"
        )

    extra_columns = raw.get("extra_columns", "raise")
    if (
        not isinstance(extra_columns, str)
        or extra_columns not in EXTRA_COLUMNS_POLICIES
    ):
        raise ContractError(
            f"{label}: 'extra_columns' must be one of {sorted(EXTRA_COLUMNS_POLICIES)}, "
            f"got {extra_columns!r}"
        )

    reads = raw.get("reads")
    if kind == "python-csv":
        if not isinstance(reads, dict) or set(reads) != {"csv"}:
            raise ContractError(
                f"{label}: a python-csv node's 'reads' must be exactly "
                "{csv: <uri>}"
            )
        try:
            parse_csv_uri(reads["csv"])
        except (ValueError, TypeError) as exc:
            raise ContractError(f"{label}: invalid csv uri: {exc}") from exc
    else:
        if not isinstance(reads, dict) or not reads:
            raise ContractError(
                f"{label}: 'reads' must be a non-empty mapping of name -> SQL"
            )
        for name, sql in reads.items():
            if not isinstance(name, str) or not name.strip():
                raise ContractError(
                    f"{label}: 'reads' name {name!r} must be a non-empty string"
                )
            if not isinstance(sql, str) or not sql.strip():
                raise ContractError(
                    f"{label}: 'reads.{name}' must be a non-empty SQL string"
                )
            if not check_reads:
                continue
            try:
                ensure_single_read(sql, dialect)
            except (ValueError, TokenError) as exc:
                # ensure_single_read's own message is phrased for check_binds
                # (its only other caller today), so it's wrapped rather than
                # surfaced bare here. TokenError is also caught: an unterminated
                # string literal or comment fails sqlglot's tokenizer with a
                # TokenError, a SqlglotError sibling of ParseError and not a
                # subclass of ValueError -- despite ensure_single_read's
                # docstring promising every rejection is a ValueError. Only
                # TokenError, not the broader SqlglotError, is caught here: by
                # the time control reaches this point `dialect` has already
                # been validated once in load_contract_dir, so any other
                # SqlglotError a future sqlglot version might raise from this
                # call should surface as itself, not get relabeled as a
                # rejected read.
                raise ContractError(
                    f"{label}: 'reads.{name}' must be a single read query ({exc})"
                ) from exc

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
        unknown_col_keys = set(entry) - _ALLOWED_OUTPUT_COLUMN_KEYS
        if unknown_col_keys:
            raise ContractError(
                f"{label}: output column has unknown key(s) {sorted(unknown_col_keys)}"
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ContractError(f"{label}: output column missing non-empty 'name'")
        col_type = entry.get("type")
        if not isinstance(col_type, str):
            raise ContractError(
                f"{label}: output column '{name}' has unsupported 'type' {col_type!r}"
            )
        try:
            parse_sql_type(col_type)
        except ContractError as e:
            raise ContractError(
                f"{label}: output column '{name}' has unsupported {str(e)}"
            ) from e
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

    config = _validate_config(raw.get("config"), label)

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
        config=config,
        content_hash=content_hash,
        kind=kind,
    )


def load_contract_dir(
    path: Path, *, dialect: str | None = None, check_reads: bool = True
) -> list[Node]:
    """Load and validate every `*.yml`/`*.yaml` contract file under ``path``.

    ``check_reads=False`` skips the per-read
    :func:`~continuo_engine_contract.sql.ensure_single_read` gate and
    nothing else; every other rule in :func:`parse_node` and every rule here
    (dialect validity, document shape, duplicate relations, "no contract
    files found") still runs. The runtime
    (:func:`~continuo_python_runtime.harness.run_node`) passes it, because
    re-running the read-shape gate at container start can only introduce a
    disagreement, never catch a new problem:

    - CI already gated the reads (``continuo-runtime validate``), under the
      repo's own ``--dialect``, and Continuo gated them again with its own
      parser and bind-check before promoting the release.
    - The runtime has no ``--dialect`` of its own, so it would re-parse with
      the dialect-neutral grammar — a *different* and in places stricter
      grammar than CI used. Ordinary postgres (``a ~ 'x'``, ``data @>
      '{...}'``) parses under ``--dialect postgres`` and not under the
      neutral parser, so a team following the docs would get a green
      validate, a green merge, an accepted release, and a node that fails on
      every single run.
    - Nothing at run time consumes the parse: ``ctx.read`` resolves declared
      reads by name and hands the SQL to the adapter verbatim.

    ``dialect`` is validated once, up front, then forwarded to
    :func:`parse_node` for every node, so every declared read is checked
    against that sqlglot dialect (``None`` -- the default -- uses sqlglot's
    dialect-neutral parser). Validating it here rather than leaving it to
    the per-read ``ensure_single_read`` call matters: an unrecognized
    dialect name (e.g. a typo'd ``--dialect POSTGRES`` instead of
    ``postgres``) raises the same bare ``ValueError`` a genuinely
    unparseable read would, and would otherwise be misreported as a
    rejected read instead of a bad flag -- blaming an innocent, valid read.

    Raises `ContractError` if ``dialect`` is not a sqlglot dialect name, if
    no nodes are found, if any file's document is malformed, or if two nodes
    across files share the same `(schema, table)`.
    """
    if dialect is not None:
        try:
            Dialect.get_or_raise(dialect)
        except ValueError as exc:
            raise ContractError(f"unknown --dialect {dialect!r}: {exc}") from exc

    files = sorted(path.glob("*.yml")) + sorted(path.glob("*.yaml"))

    nodes: list[Node] = []
    relation_sources: dict[str, str] = {}

    for file in files:
        text = file.read_text()
        try:
            document = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 — SafeLoader subclass
        except ContractError as exc:
            raise ContractError(f"{file.name}: {exc}") from None
        except yaml.YAMLError as exc:
            raise ContractError(f"{file.name}: invalid YAML: {exc}") from None
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
            node = parse_node(
                raw_node, file.name, dialect=dialect, check_reads=check_reads
            )
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
