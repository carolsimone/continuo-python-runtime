"""``continuo-runtime pack``: the bundle of a service's ODCS contracts.

A service's CI packs the contracts of its ``contracts/`` folder into one
``odcs-bundle.json`` and uploads it with the release. The bundle is UTF-8 JSON,
written by :func:`write_bundle` as ``json.dumps(doc, sort_keys=True, indent=2,
ensure_ascii=False)`` and a newline, and holds::

    {"bundle_version": 1,
     "service": <service>,
     "producer": {"continuo_python_runtime": <version>, "continuo_contracts": <version>},
     "image_packages": {<name>: <version>},
     "nodes": [{"path": <repo path of the contract file>,
                "text": <the file's text as written>,
                "source_hash": <bare hex>,
                "shared_code_hash": <bare hex, or "" when the node has none>}]}

``nodes`` is sorted by ``path``. A node's ``source_hash`` and ``shared_code_hash``
are the two parts :func:`~continuo_python_runtime.hashing.hash_parts` computes from
the files of the repository; the third part, the config hash, is computed from the
contract text by whoever reads the bundle. ``image_packages`` is the package list of
the image the service runs, read from that image, never from the environment ``pack``
runs in.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import uuid
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path
from typing import Any

from continuo_contracts.validate import validate_python_contract

from continuo_python_runtime.contract.loader import (
    issue_lines,
    load_contract_dir,
    parse_contract,
    read_contract_text,
)
from continuo_python_runtime.contract.model import Node
from continuo_python_runtime.errors import ContractError
from continuo_python_runtime.hash_inputs import gather_hash_inputs
from continuo_python_runtime.hashing import hash_parts
from continuo_python_runtime.lint import lint_source

BUNDLE_VERSION = 1

PACKAGE_SOURCE_USAGE = (
    "pack records the package list of the image the service runs: give exactly one of "
    "--image <ref> (read from the image) or --image-packages <file> "
    "(a JSON object of package name to version)"
)

# What `docker run` exits with when docker itself, not the container, failed.
_DOCKER_OWN_FAILURE = 125
_STARTUP_HOOKS = ("sitecustomize.py", "usercustomize.py")


def _lint_node_closure(
    node: Node,
    repo_root: Path,
    code_files: tuple[tuple[Path, bytes], ...],
) -> None:
    """Lint the node's script and every resolved closure member.

    Every file that executes for this node — the script plus its transitive
    in-repo import closure — is held to the same lint rules
    (``continuo_python_runtime.lint.lint_source``: L1 forbidden driver
    imports, L2 SQL string literals, L3 forbidden data-access calls, L4
    private-attribute access, L5 dynamic-import constructs, L6 constructs
    that run code the hash cannot see) that CI's
    ``continuo-runtime lint`` applies to ``scripts/``. Enforcing it again
    here, at pack time, means a helper file outside ``scripts/`` — which
    the CI lint step never looks at — cannot become a side channel around
    the harness's sole write sink, and the check cannot be defeated by a
    stale or hand-edited workflow file: whatever produces the bundle is the
    thing that checked.

    Paths in violation messages are repo-root-relative (``str(path.relative_to
    (repo_root))``), so a message reads ``lib/shared.py:1: ...`` identically
    in CI and on a laptop, never an absolute path that differs between the
    two.

    Raises ``ContractError`` naming the node and listing every violation
    from every offending file — not just the first file's — so an author
    can act on the full report in one pass instead of an iterative guessing
    game.

    Every file is decoded as ``"utf-8-sig"`` (identical to plain ``"utf-8"``
    except that a leading BOM, if present, is stripped rather than kept as a
    literal ``U+FEFF`` character) — this matches what ``ast.parse(bytes)``
    already does for the same file in ``resolve_closure`` (bytes-mode
    parsing treats a UTF-8 BOM as an implicit encoding declaration and
    strips it), so a BOM file that parses and runs fine is not rejected here
    on a mismatch between the two decoders. A file that isn't valid UTF-8 at
    all (e.g. a PEP 263 ``# -*- coding: latin-1 -*-`` cookie plus a
    non-ASCII byte — valid input to ``ast.parse(bytes)``, which honors the
    cookie) raises ``UnicodeDecodeError``; that is caught and re-raised as a
    ``ContractError`` naming the node and the offending repo-relative path,
    so it joins the same error taxonomy as every other rejection here
    instead of escaping as a bare stdlib exception past ``cli.main``, which
    catches only ``HarnessError``/``OSError``.
    """
    repo_root_resolved = repo_root.resolve()
    violations: list[str] = []
    for path, data in code_files:
        rel = path.relative_to(repo_root_resolved)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ContractError(f"{node.relation}: {rel}: not valid UTF-8: {exc}") from exc
        violations.extend(lint_source(text, str(rel)))

    if violations:
        report = "\n".join(violations)
        raise ContractError(
            f"{node.relation}: lint violations in script or import closure:\n{report}"
        )


def _refuse_startup_hooks(repo_root: Path) -> None:
    """Refuse a ``sitecustomize.py`` or ``usercustomize.py`` at the repository root.

    Python imports either at startup, before the harness, so nothing in a node's
    import closure reaches it and the hash cannot see it.
    """
    for hook in _STARTUP_HOOKS:
        if (repo_root / hook).exists():
            raise ContractError(
                f"{hook} at the repository root runs before the harness and is not in the hash"
            )


def _repo_path(file: Path, repo_root: Path) -> str:
    """The POSIX path of the contract ``file`` below ``repo_root``, as the bundle names it.

    The folder of ``file`` and ``repo_root`` are resolved, so the path does not
    depend on how either is spelled (relative, through a symlink). Raises
    ``ContractError`` for a contract outside the repository root.
    """
    resolved = file.parent.resolve() / file.name
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        raise ContractError(
            f"{file}: the contract is not inside the repository root {repo_root}"
        ) from None


def build_bundle(
    contract_dir: Path,
    repo_root: Path,
    service: str,
    image_packages: Mapping[str, str],
    *,
    dialect: str | None = None,
) -> dict[str, Any]:
    """Build and return the bundle of the contracts in ``contract_dir``.

    The contracts are loaded as :func:`~continuo_python_runtime.contract.loader
    .load_contract_dir` loads them, every read checked under ``dialect`` (``None``
    is sqlglot's dialect-neutral parser). Each contract is then checked once more
    against ``service`` by continuo-contracts, as the release does: one
    ``ContractError`` lists every issue of every file, one line each, naming the
    file by its path below ``repo_root``.

    Each node's script, in-repo import closure and assets are gathered from
    ``repo_root``, every file that executes is linted by :func:`_lint_node_closure`
    and the two hash parts of the bundle are computed.

    Raises ``ContractError`` for any refusal above, for a ``sitecustomize.py`` or
    ``usercustomize.py`` at ``repo_root``, for a script or asset that is missing or
    leaves ``repo_root``, and for a contract outside ``repo_root``.
    """
    nodes = load_contract_dir(contract_dir, dialect=dialect, check_reads=True)
    _refuse_startup_hooks(repo_root)

    files = [(node, _repo_path(node.path, repo_root), read_contract_text(node.path)) for node in nodes]
    issues: list[str] = []
    for _, path, text in files:
        found = validate_python_contract(parse_contract(text, path), path=path, service=service)
        issues.extend(issue_lines(path, found))
    if issues:
        raise ContractError("\n".join(issues))

    entries = []
    for node, path, text in files:
        inputs = gather_hash_inputs(node, repo_root)
        _lint_node_closure(node, repo_root, inputs.code_files)
        parts = hash_parts(node, inputs.primary, inputs.units)
        entries.append(
            {
                "path": path,
                "text": text,
                "source_hash": parts["source_hash"],
                "shared_code_hash": parts["shared_code_hash"],
            }
        )
    entries.sort(key=lambda entry: entry["path"])

    return {
        "bundle_version": BUNDLE_VERSION,
        "service": service,
        "producer": {
            "continuo_python_runtime": metadata.version("continuo-python-runtime"),
            "continuo_contracts": metadata.version("continuo-contracts"),
        },
        "image_packages": dict(image_packages),
        "nodes": entries,
    }


def write_bundle(doc: Mapping[str, Any], out: Path) -> None:
    """Write ``doc`` to ``out`` as the bundle's JSON, creating ``out``'s missing folders.

    The JSON goes to a temporary file in ``out``'s folder, which is renamed over
    ``out`` once written, so a failure leaves no partial ``out`` (and a file that
    was there stays as it was) and no temporary file.
    """
    data = (json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(f".{out.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, out)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        raise


def _parse_packages(text: str, source: str) -> dict[str, str]:
    """Return the package list in ``text``: one JSON object of name to version strings.

    ``source`` names where the text came from, for the refusals.
    """
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise ContractError(f"{source} is not JSON: {exc}") from None
    if not isinstance(value, dict) or not all(isinstance(version, str) for version in value.values()):
        raise ContractError(f"{source} must be one JSON object of name to version strings")
    return value


def _packages_from_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ContractError(f"{path}: cannot read the package list ({exc})") from None
    return _parse_packages(text, str(path))


def _packages_from_image(image: str) -> dict[str, str]:
    command = ["docker", "run", "--rm", "--entrypoint", "continuo-runtime", image, "packages"]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
        )
    except OSError as exc:
        raise ContractError(
            f"{image}: cannot read the package list of the image, `docker` could not be run ({exc})"
        ) from None
    if result.returncode != 0:
        detail = result.stderr.strip() or "no output"
        if result.returncode == _DOCKER_OWN_FAILURE:
            raise ContractError(f"{image}: docker could not run the image: {detail}")
        raise ContractError(
            f"{image}: the image has no `packages` command: build it on continuo-python-runtime "
            f"0.13.0 or later (`docker run` exited with status {result.returncode}: {detail})"
        )
    return _parse_packages(result.stdout, f"{image}: the output of its `packages` command")


def image_packages_from(*, image: str | None, image_packages: Path | None) -> dict[str, str]:
    """Return the package list ``pack`` records, from exactly one of two sources.

    ``image`` is an image reference: its list is the output of ``docker run --rm
    --entrypoint continuo-runtime <image> packages``. ``image_packages`` is a file
    that holds that output, one JSON object of package name to version string.

    Raises ``ContractError`` when neither or both are given, and, for an image,
    when ``docker`` cannot be run, exits with a non-zero status (the image has no
    ``packages`` command, or docker could not run it) or prints anything but one
    JSON object of strings; for a file, when it cannot be read or does not hold one.
    """
    if image is not None and image_packages is None:
        return _packages_from_image(image)
    if image is None and image_packages is not None:
        return _packages_from_file(image_packages)
    raise ContractError(PACKAGE_SOURCE_USAGE)
