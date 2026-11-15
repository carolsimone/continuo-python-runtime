"""CLI for continuo-runtime: validate, merge, hash, lint, and run contract workflows."""

import argparse
import logging
import os
import sys
from importlib import metadata
from pathlib import Path

from continuo_python_runtime.contract.loader import load_contract_dir
from continuo_python_runtime.contract.merge import (
    build_wire_contract,
    write_wire_contract,
)
from continuo_python_runtime.errors import HarnessError
from continuo_python_runtime.hash_inputs import gather_hash_inputs
from continuo_python_runtime.hashing import hash_parts
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
    parser.add_argument(
        "--version",
        action="version",
        version=f"continuo-runtime {metadata.version('continuo-python-runtime')}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    dialect_help = (
        "sqlglot dialect the reads are authored in (e.g. postgres, trino); "
        "defaults to sqlglot's dialect-neutral parser."
    )

    # validate subcommand
    validate_parser = subparsers.add_parser(
        "validate", help="Validate contract directory"
    )
    validate_parser.add_argument("contract_dir", help="Path to contract directory")
    validate_parser.add_argument("--dialect", default=None, help=dialect_help)

    # merge subcommand
    merge_parser = subparsers.add_parser(
        "merge", help="Merge contracts into wire contract"
    )
    merge_parser.add_argument("contract_dir", help="Path to contract directory")
    merge_parser.add_argument("--service", required=True, help="Service name")
    merge_parser.add_argument("--repo-root", required=True, help="Repository root path")
    merge_parser.add_argument("--out", required=True, help="Output file path")
    merge_parser.add_argument("--dialect", default=None, help=dialect_help)

    # hash subcommand
    hash_parser = subparsers.add_parser(
        "hash", help="Print relation and hash for each node"
    )
    hash_parser.add_argument("contract_dir", help="Path to contract directory")
    hash_parser.add_argument(
        "--repo-root", required=True, help="Repository root path"
    )
    hash_parser.add_argument("--dialect", default=None, help=dialect_help)

    # lint subcommand
    lint_parser = subparsers.add_parser(
        "lint", help="Lint Python scripts for forbidden imports and SQL literals"
    )
    lint_parser.add_argument("path", nargs="+", help="Paths to lint (files or directories)")

    # run subcommand
    subparsers.add_parser(
        "run", help="Execute a node script (container entrypoint)"
    )

    # validation-op subcommand
    subparsers.add_parser(
        "validation-op",
        help="Execute one blue/green validation operation (container command; "
             "driven by VALIDATION_OP and the engine Secret env)",
    )

    args = parser.parse_args(argv)

    try:
        if args.command == "validate":
            return cmd_validate(args.contract_dir, args.dialect)
        elif args.command == "merge":
            return cmd_merge(
                args.contract_dir, args.service, args.repo_root, args.out, args.dialect
            )
        elif args.command == "hash":
            return cmd_hash(args.contract_dir, args.repo_root, args.dialect)
        elif args.command == "lint":
            return cmd_lint(args.path)
        elif args.command == "run":
            return cmd_run()
        elif args.command == "validation-op":
            from continuo_python_runtime.validation.runner import main as validation_op_main
            validation_op_main()
            return 0
    except HarnessError as exc:
        logger.error("%s", exc)
        return 1
    except OSError as exc:
        logger.error("%s", exc)
        return 1

    return 0


def cmd_validate(contract_dir: str, dialect: str | None = None) -> int:
    """Validate contracts in a directory."""
    load_contract_dir(Path(contract_dir), dialect=dialect)
    return 0


def cmd_merge(
    contract_dir: str,
    service: str,
    repo_root: str,
    out: str,
    dialect: str | None = None,
) -> int:
    """Merge contracts into a wire contract file."""
    doc = build_wire_contract(Path(contract_dir), Path(repo_root), service, dialect=dialect)
    write_wire_contract(doc, Path(out))
    return 0


def cmd_hash(contract_dir: str, repo_root: str, dialect: str | None = None) -> int:
    """Print ``<relation>\\t<content_hash>`` for each node, one line each, sorted by relation.

    Each node is the one a contract in ``contract_dir`` declares, hashed from the
    files of ``repo_root`` it names. Every node is hashed before the first line
    is printed, so a node that cannot be hashed leaves stdout empty.
    """
    root = Path(repo_root)
    hashes = {}
    for node in load_contract_dir(Path(contract_dir), dialect=dialect):
        inputs = gather_hash_inputs(node, root)
        hashes[node.relation] = hash_parts(node, inputs.primary, inputs.units)["content_hash"]

    for relation, content_hash in sorted(hashes.items()):
        print(f"{relation}\t{content_hash}")

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
