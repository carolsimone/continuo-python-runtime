"""CLI for continuo-runtime: validate, merge, hash, lint, and run contract workflows."""

import argparse
import logging
import os
import sys
from pathlib import Path

from continuo_python_runtime.contract.loader import load_contract_dir
from continuo_python_runtime.contract.merge import (
    build_wire_contract,
    write_wire_contract,
)
from continuo_python_runtime.errors import HarnessError
from continuo_python_runtime.lint import lint_paths
from continuo_python_runtime import harness

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

    # lint subcommand
    lint_parser = subparsers.add_parser(
        "lint", help="Lint Python scripts for forbidden imports and SQL literals"
    )
    lint_parser.add_argument("path", nargs="+", help="Paths to lint (files or directories)")

    # run subcommand
    subparsers.add_parser(
        "run", help="Execute a node script (container entrypoint)"
    )

    args = parser.parse_args(argv)

    try:
        if args.command == "validate":
            return cmd_validate(args.contract_dir)
        elif args.command == "merge":
            return cmd_merge(args.contract_dir, args.service, args.repo_root, args.out)
        elif args.command == "hash":
            return cmd_hash(args.contract_dir, args.repo_root)
        elif args.command == "lint":
            return cmd_lint(args.path)
        elif args.command == "run":
            return cmd_run()
    except HarnessError as exc:
        logger.error("%s", exc)
        return 1
    except OSError as exc:
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
    """Print relation and content hash for each node.

    Reuses build_wire_contract for consistent hashing and error handling.
    Service value is irrelevant to per-node hashes since entries don't include it.
    """
    # Reuse build_wire_contract for consistent hashing and error handling
    doc = build_wire_contract(Path(contract_dir), Path(repo_root), service="_hash")

    # Print relation\thash from wire contract nodes (already sorted by relation)
    for node_entry in doc["nodes"]:
        relation = f"{node_entry['schema']}.{node_entry['table']}"
        hash_value = node_entry["content_hash"]
        print(f"{relation}\t{hash_value}")

    return 0


def cmd_lint(paths: list[str]) -> int:
    """Lint Python scripts for forbidden imports and SQL literals.

    Reports violations to stderr via logging and exits with 1 if any are found.
    """
    violations = lint_paths([Path(p) for p in paths])
    if violations:
        for violation in violations:
            logger.error("%s", violation)
        return 1
    return 0


def cmd_run() -> int:
    """Execute a node script reading configuration from os.environ.

    This is the container entrypoint: reads NODE_ID, TABLE_NAME, TARGET_SCHEMA,
    CONTRACT_DIR, and APP_ROOT from environment, then calls harness.run_node()
    which produces exactly one sentinel result block.
    """
    return harness.run_node(os.environ)
