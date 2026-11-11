"""Contract v1 merger: wire contract builder."""

from pathlib import Path

import yaml

from continuo_python_runtime.contract.loader import load_contract_dir
from continuo_python_runtime.contract.model import CONTRACT_VERSION, DEFAULT_MODE_TYPE, Node
from continuo_python_runtime.errors import ContractError
from continuo_python_runtime.hash_inputs import gather_hash_inputs
from continuo_python_runtime.hashing import hash_parts
from continuo_python_runtime.lint import lint_source


def node_entry(node: Node) -> dict:
    """Convert a Node to its wire form dict.

    Returns the node as a dict with all fields, output_columns as list of
    dicts. The entry carries no hash fields: build_wire_contract adds all
    four (source_hash, shared_code_hash, config_hash, content_hash). The
    nullable field is always present in output_columns. ``secret_ref``,
    ``mode_type``, ``primary_key`` and ``assets`` appear only when they differ
    from their default, so the entry of a node that does not use them stays
    minimal.
    """
    entry = {
        "schema": node.schema,
        "table": node.table,
        "owner": node.owner,
        "schedule": node.schedule,
        "criticality": node.criticality,
        "kind": node.kind,
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
        "config": dict(node.config),
    }
    # Only a kind that may name a Secret ever carries one; the key is emitted
    # only when set.
    if node.secret_ref:
        entry["secret_ref"] = node.secret_ref
    # Same reasoning: a replace node's entry carries no mode_type.
    if node.mode_type != DEFAULT_MODE_TYPE:
        entry["mode_type"] = node.mode_type
    if node.mode_type == "upsert":
        entry["primary_key"] = list(node.primary_key)
    if node.assets:
        entry["assets"] = list(node.assets)
    return entry


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
    private-attribute access, L5 dynamic-import constructs) that CI's
    ``continuo-runtime lint`` applies to ``scripts/``. Enforcing it again
    here, at merge time, means a helper file outside ``scripts/`` — which
    the CI lint step never looks at — cannot become a side channel around
    the harness's sole write sink, and the check cannot be defeated by a
    stale or hand-edited workflow file: whatever produces the release
    artifact is the thing that checked.

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


def build_wire_contract(
    contract_dir: Path, repo_root: Path, service: str, *, dialect: str | None = None
) -> dict:
    """Build and return a wire contract document.

    Loads contracts from contract_dir, resolves script paths against repo_root,
    computes content hashes, and returns a contract document sorted by relation.
    ``dialect`` is forwarded to :func:`~continuo_python_runtime.contract.loader
    .load_contract_dir` so every declared read is checked against that sqlglot
    dialect (``None`` -- the default -- uses sqlglot's dialect-neutral parser).

    Raises ContractError if any script file is missing.
    """
    nodes = load_contract_dir(contract_dir, dialect=dialect)

    wire_nodes = []
    for node in nodes:
        entry = node_entry(node)

        inputs = gather_hash_inputs(node, repo_root)
        _lint_node_closure(node, repo_root, inputs.code_files)
        entry.update(hash_parts(node, inputs.primary, inputs.units))
        # The one wire key outside config_hash: it is written after the hashes
        # are computed, so changing it alone never changes content_hash.
        if node.timeout_minutes is not None:
            entry["timeout_minutes"] = node.timeout_minutes

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
