"""Build a node's ODCS contract in tests, from the fields a test names.

A test describes a node with the field names of the runtime's `Node` (`schema`,
`table`, `owner`, `reads`, `output_columns`, `mode_type`, `primary_key`, ...).
`contract_text` renders the ODCS v3.2.0 contract that declares that node, as
the YAML text of a `<table>.odcs.yaml` file. `write_contract` writes that file
into a contracts folder, and `contract_node` returns the `Node` the loader
reads from it.

Each field maps to one place of the contract:

- `schema` and `table` give the `id` (`<schema>.<table>`), the `name` and the
  name of the one schema object; `owner` is `team.name`; `description` is
  `description.purpose`.
- `output_columns` are the properties of the schema object. Each is a mapping
  of `name`, `type` and, optionally, `nullable` (true when left out): `type` is
  the column's `physicalType` and a column with `nullable: False` is
  `required: true`.
- `primary_key` sets `primaryKey: true` on the columns it names, with a
  `primaryKeyPosition` from 1 in the order given when it names more than one.
- `reads` is the `readQueries` property, or, on a `python-csv` node, the
  `csvSource` property, which `reads` then gives as `{"csv": <uri>}`.
- `kind`, `schedule`, `criticality`, `timeout_minutes`, `script`,
  `extra_columns`, `config`, `secret_ref`, `mode_type` and `assets` are the
  `continuo` custom properties `kind`, `schedule`, `criticality`,
  `timeoutMinutes`, `script`, `extraColumns`, `config`, `secretRef`,
  `modeType` and `assets`.

A field left as `None` is not written, so the contract takes the library's
default for it, or the library refuses the contract when the field is one it
needs.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from continuo_contracts import yaml_strict

from continuo_python_runtime.contract.loader import node_from_contract
from continuo_python_runtime.contract.model import Node

_COLUMN_KEYS = {"name", "type", "nullable"}


def contract_text(
    *,
    schema: str,
    table: str,
    owner: str,
    schedule: str,
    criticality: str,
    output_columns: Sequence[Mapping[str, Any]],
    kind: str | None = None,
    script: str | None = None,
    reads: Mapping[str, str] | None = None,
    description: str | None = None,
    extra_columns: str | None = None,
    config: Mapping[str, Any] | None = None,
    secret_ref: str | None = None,
    mode_type: str | None = None,
    primary_key: Sequence[str] | None = None,
    timeout_minutes: int | None = None,
    assets: Sequence[str] | None = None,
) -> str:
    """Return the YAML text of the ODCS contract that declares the node these fields describe.

    Raises `ValueError` for fields the contract cannot hold: a column with a key
    other than `name`, `type` and `nullable`, a `primary_key` name that is no
    column, or the `reads` of a `python-csv` node that is not `{"csv": <uri>}`.
    """
    custom: list[tuple[str, Any]] = [
        ("kind", kind),
        ("schedule", schedule),
        ("criticality", criticality),
        ("timeoutMinutes", timeout_minutes),
        ("script", script),
    ]
    if kind == "python-csv" and reads is not None:
        if set(reads) != {"csv"}:
            raise ValueError(f"the reads of a python-csv node are {{'csv': <uri>}}, not {reads!r}")
        custom.append(("csvSource", reads["csv"]))
    else:
        custom.append(("readQueries", None if reads is None else dict(reads)))
    custom += [
        ("extraColumns", extra_columns),
        ("config", None if config is None else dict(config)),
        ("secretRef", secret_ref),
        ("modeType", mode_type),
        ("assets", None if assets is None else list(assets)),
    ]

    doc: dict[str, Any] = {
        "apiVersion": "v3.2.0",
        "kind": "DataContract",
        "id": f"{schema}.{table}",
        "name": table,
        "version": "0",
    }
    if description is not None:
        doc["description"] = {"purpose": description}
    doc["team"] = {"name": owner}
    doc["customProperties"] = [
        {"vendor": "continuo", "property": name, "value": value}
        for name, value in custom
        if value is not None
    ]
    doc["schema"] = [
        {
            "name": table,
            "physicalType": "table",
            "properties": _columns(output_columns, primary_key or ()),
        }
    ]
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def write_contract(contracts_dir: Path, **fields: Any) -> Path:
    """Write `contract_text(**fields)` to `<contracts_dir>/<table>.odcs.yaml` and return its path.

    The folder is created when it does not exist.
    """
    contracts_dir.mkdir(parents=True, exist_ok=True)
    path = contracts_dir / f"{fields['table']}.odcs.yaml"
    path.write_text(contract_text(**fields), encoding="utf-8")
    return path


def contract_node(**fields: Any) -> Node:
    """Return the `Node` the loader reads from `contract_text(**fields)`.

    The contract is read as the file `<table>.odcs.yaml`, as the harness reads
    it: every rule applies but the reads are not parsed as SQL
    (`check_reads=False`).
    """
    text = contract_text(**fields)
    return node_from_contract(
        yaml_strict.load(text),
        Path(f"{fields['table']}.odcs.yaml"),
        dialect=None,
        check_reads=False,
    )


def _columns(
    output_columns: Sequence[Mapping[str, Any]], primary_key: Sequence[str]
) -> list[dict[str, Any]]:
    names = [column["name"] for column in output_columns]
    for name in primary_key:
        if name not in names:
            raise ValueError(f"primary_key names {name!r}, which is not an output column")
    columns = []
    for column in output_columns:
        unknown = set(column) - _COLUMN_KEYS
        if unknown:
            raise ValueError(f"an output column holds {sorted(unknown)}, it takes only {sorted(_COLUMN_KEYS)}")
        prop: dict[str, Any] = {"name": column["name"], "physicalType": column["type"]}
        if column.get("nullable", True) is False:
            prop["required"] = True
        if column["name"] in primary_key:
            prop["primaryKey"] = True
            if len(primary_key) > 1:
                prop["primaryKeyPosition"] = list(primary_key).index(column["name"]) + 1
        columns.append(prop)
    return columns
