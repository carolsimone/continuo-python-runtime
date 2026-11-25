"""CLI for continuo-runtime: validate, pack, hash, packages, lint, run and validation-op."""

import argparse
import json
import logging
import os
import sys
from importlib import metadata
from pathlib import Path

from continuo_python_runtime.contract.loader import load_contract_dir, load_contract_docs
from continuo_python_runtime.contract.ontology_check import check_project
from continuo_python_runtime.contract.pack import (
    PACKAGE_SOURCE_USAGE,
    build_bundle,
    image_packages_from,
    write_bundle,
)
from continuo_python_runtime.errors import HarnessError
from continuo_python_runtime.hash_inputs import gather_hash_inputs
from continuo_python_runtime.hashing import hash_parts
from continuo_python_runtime.lint import lint_paths
from continuo_python_runtime.packages import installed_packages
from continuo_python_runtime import harness

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Run the subcommand ``argv`` names and return its exit code.

    A usage error exits 2 through argparse. A :class:`HarnessError` or an
    ``OSError`` raised by a subcommand is logged and returns 1.
    """
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
    validate_parser.add_argument(
        "--project",
        default=None,
        help="The project the contracts belong to: a contract whose `project` property "
             "names another project is refused",
    )
    validate_parser.add_argument(
        "--ontology",
        default=None,
        help="The company's ontology file (needs --project): check every contract "
             "against it, as a release does",
    )

    # pack subcommand
    pack_parser = subparsers.add_parser(
        "pack",
        help="Pack the contracts into the bundle (odcs-bundle.json) a release carries",
        description=(
            "Check every contract of the folder, lint every script it runs and write "
            "the bundle. " + PACKAGE_SOURCE_USAGE + "."
        ),
    )
    pack_parser.add_argument("contract_dir", help="Path to contract directory")
    pack_parser.add_argument("--project", required=True, help="Project name")
    pack_parser.add_argument("--repo-root", required=True, help="Repository root path")
    pack_parser.add_argument(
        "--image",
        default=None,
        help="Image the project runs; its package list is read with "
             "`docker run --entrypoint continuo-runtime <image> packages`",
    )
    pack_parser.add_argument(
        "--image-packages",
        default=None,
        help="File holding the package list of that image, one JSON object of name to version",
    )
    pack_parser.add_argument("--out", required=True, help="Output file path")
    pack_parser.add_argument("--dialect", default=None, help=dialect_help)

    # hash subcommand
    hash_parser = subparsers.add_parser(
        "hash", help="Print relation and hash for each node"
    )
    hash_parser.add_argument("contract_dir", help="Path to contract directory")
    hash_parser.add_argument(
        "--repo-root", required=True, help="Repository root path"
    )
    hash_parser.add_argument("--dialect", default=None, help=dialect_help)

    # packages subcommand
    subparsers.add_parser(
        "packages",
        help="Print the installed packages of this environment as one JSON object "
             "({normalised name: version})",
    )

    # lint subcommand
    lint_parser = subparsers.add_parser(
        "lint", help="Lint Python scripts for forbidden imports and SQL literals"
    )
    lint_parser.add_argument("path", nargs="+", help="Paths to lint (files or directories)")

    # run subcommand
    subparsers.add_parser(
        "run", help="Run the node the environment names (container entrypoint)"
    )

    # validation-op subcommand
    subparsers.add_parser(
        "validation-op",
        help="Execute one blue/green validation operation (container command; "
             "driven by VALIDATION_OP and the engine Secret env)",
    )

    args = parser.parse_args(argv)
    if args.command == "validate" and args.ontology is not None and args.project is None:
        validate_parser.error("--ontology needs --project")
    if args.command == "pack" and (args.image is None) == (args.image_packages is None):
        pack_parser.error(PACKAGE_SOURCE_USAGE)

    try:
        if args.command == "validate":
            return cmd_validate(
                args.contract_dir,
                args.dialect,
                project=args.project,
                ontology=args.ontology,
            )
        elif args.command == "pack":
            return cmd_pack(
                args.contract_dir,
                args.project,
                args.repo_root,
                args.out,
                image=args.image,
                image_packages=args.image_packages,
                dialect=args.dialect,
            )
        elif args.command == "hash":
            return cmd_hash(args.contract_dir, args.repo_root, args.dialect)
        elif args.command == "packages":
            return cmd_packages()
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


_FINDING_LEVELS = {"error": logging.ERROR, "warning": logging.WARNING, "info": logging.INFO}


def cmd_validate(
    contract_dir: str,
    dialect: str | None = None,
    *,
    project: str | None = None,
    ontology: str | None = None,
) -> int:
    """Validate contracts in a directory.

    With no ``project``, every contract is checked as a contract of any project
    and nothing is logged. With a ``project``, a contract whose ``project``
    property names another one is refused too, as ``pack`` refuses it.

    With an ``ontology`` file (which needs the ``project``), each contract is
    then checked against it by the per-project rules of continuo-contracts.
    Each finding is logged at its level, ERROR for an error, WARNING for a
    warning and INFO for information, as ``<severity> [<rule>] <subject>:
    <message>``, in the order the library returns them, followed by one INFO
    line counting the three. Any error makes the exit code 1. An ontology file
    that cannot be read, is not an ontology or is refused by the library's
    ``ontology`` rule is one ERROR line and exit 1, before any finding. Nothing
    goes to stdout.
    """
    if project is None:
        load_contract_dir(Path(contract_dir), dialect=dialect)
        return 0
    loaded = load_contract_docs(Path(contract_dir), dialect=dialect, project=project)
    if ontology is None:
        return 0

    findings = check_project(
        [doc for _, doc in loaded], project=project, ontology_path=Path(ontology)
    )
    for finding in findings:
        logger.log(
            _FINDING_LEVELS[finding.severity],
            "%s [%s] %s: %s",
            finding.severity,
            finding.rule,
            finding.subject,
            finding.message,
        )
    counts = {severity: 0 for severity in _FINDING_LEVELS}
    for finding in findings:
        counts[finding.severity] += 1
    logger.info(
        "%d error(s), %d warning(s), %d info: %s",
        counts["error"],
        counts["warning"],
        counts["info"],
        project,
    )
    return 1 if counts["error"] else 0


def cmd_pack(
    contract_dir: str,
    project: str,
    repo_root: str,
    out: str,
    *,
    image: str | None,
    image_packages: str | None,
    dialect: str | None = None,
) -> int:
    """Pack the contracts into the bundle file ``out``.

    The bundle records the package list of ``image``, or the one ``image_packages``
    holds, never the packages of the environment this command runs in. Nothing is
    written to ``out`` unless every check passed.
    """
    packages = image_packages_from(
        image=image, image_packages=None if image_packages is None else Path(image_packages)
    )
    doc = build_bundle(Path(contract_dir), Path(repo_root), project, packages, dialect=dialect)
    write_bundle(doc, Path(out))
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


def cmd_packages() -> int:
    """Print every installed package as one JSON object, ``{normalised name: version}``, and a newline.

    The object is the only thing on stdout: a caller that runs this command in an image
    parses it as the image's package list.
    """
    print(json.dumps(installed_packages(), sort_keys=True))
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
    """Run the node ``os.environ`` names and return its exit code.

    This is the container entrypoint. ``harness.run_node`` reads ``NODE_ID``,
    ``TABLE_NAME`` and ``TARGET_SCHEMA``, and, when set, ``CONTRACT_DIR`` (the
    folder of the image's ``*.odcs.yaml`` contracts) and ``APP_ROOT`` (the
    repository root a script path is resolved against), runs the node and
    prints exactly one sentinel result block.
    """
    return harness.run_node(os.environ)
