"""Contract v1 merger: wire contract builder."""

from pathlib import Path

import yaml

from continuo_python_runtime.contract.loader import load_contract_dir
from continuo_python_runtime.contract.model import CONTRACT_VERSION, Node
from continuo_python_runtime.errors import ContractError
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

        # Reject absolute script paths
        if Path(node.script).is_absolute():
            raise ContractError(
                f"{node.relation}: script path {node.script!r} must be relative to the repository root"
            )

        # Resolve script path against repo_root and enforce containment
        script_path = (repo_root / node.script).resolve()
        if not script_path.is_relative_to(repo_root.resolve()):
            raise ContractError(
                f"{node.relation}: script path {node.script!r} escapes the repository root"
            )
        if not script_path.is_file():
            raise ContractError(f"script not found: {node.script}")

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
    """Write a wire contract document to a YAML file."""
    out.write_text(yaml.safe_dump(doc, sort_keys=False))
