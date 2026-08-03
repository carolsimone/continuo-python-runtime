"""Contract v1 merger: wire contract builder."""

from pathlib import Path

import yaml

from continuo_python_runtime.contract.loader import load_contract_dir
from continuo_python_runtime.contract.model import CONTRACT_VERSION, Node
from continuo_python_runtime.contract.paths import resolve_script_path
from continuo_python_runtime.hashing import content_hash


def node_entry(node: Node) -> dict:
    """Convert a Node to its wire form dict.

    Returns the node as a dict with all fields, output_columns as list of dicts,
    and NO content_hash. The nullable field is always present in output_columns.
    """
    return {
        "schema": node.schema,
        "table": node.table,
        "owner": node.owner,
        "schedule": node.schedule,
        "criticality": node.criticality,
        "script": node.script,
        "reads": node.reads,
        "output_columns": [
            {
                "name": col.name,
                "type": col.type,
                "nullable": col.nullable,
            }
            for col in node.output_columns
        ],
        "description": node.description,
        "extra_columns": node.extra_columns,
    }


def build_wire_contract(contract_dir: Path, repo_root: Path, service: str) -> dict:
    """Build and return a wire contract document.

    Loads contracts from contract_dir, resolves script paths against repo_root,
    computes content hashes, and returns a contract document sorted by relation.

    Raises ContractError if any script file is missing.
    """
    nodes = load_contract_dir(contract_dir)

    wire_nodes = []
    for node in nodes:
        entry = node_entry(node)

        script_path = resolve_script_path(node.script, repo_root, context=node.relation)

        # Read script bytes and compute hash
        script_bytes = script_path.read_bytes()
        hash_value = content_hash(entry, script_bytes)
        entry["content_hash"] = hash_value

        wire_nodes.append(entry)

    # Sort by relation (schema.table)
    wire_nodes.sort(key=lambda entry: f"{entry['schema']}.{entry['table']}")

    return {
        "contract_version": CONTRACT_VERSION,
        "service": service,
        "nodes": wire_nodes,
    }


def write_wire_contract(doc: dict, out: Path) -> None:
    """Write a wire contract document to a YAML file.

    Creates ``out``'s parent directory (and any missing ancestors) first, so
    callers don't need to pre-create the output directory.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(doc, sort_keys=False))
