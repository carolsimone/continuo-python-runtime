"""CLI for continuo-runtime: validate, merge, and hash contract workflows."""

import argparse
import logging
import sys
from pathlib import Path

from continuo_python_runtime.contract.loader import load_contract_dir
from continuo_python_runtime.contract.merge import (
    build_wire_contract,
    write_wire_contract,
)
from continuo_python_runtime.errors import HarnessError
from continuo_python_runtime.hashing import content_hash

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Main CLI entry point with validate, merge, and hash subcommands."""
    # Configure logging once
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(prog="continuo-runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # validate subcommand
    validate_parser = subparsers.add_parser(
        "validate", help="Validate contract directory"
    )
    validate_parser.add_argument("contract_dir", help="Path to contract directory")

    # merge subcommand
    merge_parser = subparsers.add_parser(
        "merge", help="Merge contracts into wire contract"
    )
    merge_parser.add_argument("contract_dir", help="Path to contract directory")
    merge_parser.add_argument("--service", required=True, help="Service name")
    merge_parser.add_argument("--repo-root", required=True, help="Repository root path")
    merge_parser.add_argument("--out", required=True, help="Output file path")

    # hash subcommand
    hash_parser = subparsers.add_parser(
        "hash", help="Print relation and hash for each node"
    )
    hash_parser.add_argument("contract_dir", help="Path to contract directory")
    hash_parser.add_argument(
        "--repo-root", required=True, help="Repository root path"
    )

    args = parser.parse_args(argv)

    try:
        if args.command == "validate":
            return cmd_validate(args.contract_dir)
        elif args.command == "merge":
            return cmd_merge(args.contract_dir, args.service, args.repo_root, args.out)
        elif args.command == "hash":
            return cmd_hash(args.contract_dir, args.repo_root)
    except HarnessError as exc:
        logger.error("%s", exc)
        return 1

    return 0


def cmd_validate(contract_dir: str) -> int:
    """Validate contracts in a directory."""
    load_contract_dir(Path(contract_dir))
    return 0


def cmd_merge(contract_dir: str, service: str, repo_root: str, out: str) -> int:
    """Merge contracts into a wire contract file."""
    doc = build_wire_contract(Path(contract_dir), Path(repo_root), service)
    write_wire_contract(doc, Path(out))
    return 0


def cmd_hash(contract_dir: str, repo_root: str) -> int:
    """Print relation and content hash for each node."""
    nodes = load_contract_dir(Path(contract_dir))
    repo_root_path = Path(repo_root)

    # Sort nodes by relation for consistent output
    sorted_nodes = sorted(nodes, key=lambda n: n.relation)

    for node in sorted_nodes:
        # Resolve script path and read bytes
        script_path = (repo_root_path / node.script).resolve()
        script_bytes = script_path.read_bytes()

        # Build entry dict (similar to node_entry in merge.py)
        entry = {
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

        # Compute content hash
        hash_value = content_hash(entry, script_bytes)

        # Print relation\thash
        print(f"{node.relation}\t{hash_value}")

    return 0
