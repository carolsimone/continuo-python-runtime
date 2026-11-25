"""Check a Python project's contracts against the company's ontology file.

``continuo-runtime validate --project <name> --ontology <file>`` runs the
per-project rules of continuo-contracts over the contracts of one project:
each contract is stored as a release stores it, filled with what the
ontology knows and checked, so a team sees in its own CI what a release says
about its bindings. The rules read contracts against an ontology file that the
library's own ``ontology`` rule accepts, so a file that rule refuses stops the
check before any contract is read, as it does in the library's dbt check. This
is the only module of the runtime that imports the library's ontology modules.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from continuo_contracts import ontology
from continuo_contracts.contract import normalise_python
from continuo_contracts.errors import ContractsError, OntologyError
from continuo_contracts.ontology_check import (
    PER_PROJECT_RULES,
    Finding,
    check,
    ontology_findings,
)

from continuo_python_runtime.errors import ContractError


def _read_ontology(ontology_path: Path) -> ontology.Ontology:
    """Return the ontology the file ``ontology_path`` holds.

    Raises ``ContractError``, with the message ``<file>: <reason>``, for a file
    that cannot be read (missing, a folder, no permission), one that is not
    UTF-8 text and one the library does not accept as an ontology, whose
    reason is the library's own message. The ``ontology`` rule, which can
    refuse a file that loads, is run by :func:`check_project`.
    """
    try:
        text = ontology_path.read_bytes().decode("utf-8")
    except OSError as exc:
        raise ContractError(f"{ontology_path}: {exc.strerror or exc}") from None
    except UnicodeDecodeError as exc:
        raise ContractError(f"{ontology_path}: the file is not UTF-8 text ({exc})") from None
    try:
        return ontology.load(text)
    except OntologyError as exc:
        raise ContractError(f"{ontology_path}: {exc}") from None


def check_project(
    docs: Sequence[Mapping[str, Any]], *, project: str, ontology_path: Path
) -> list[Finding]:
    """Return the findings of the per-project rules over ``docs``, the contracts of ``project``.

    ``docs`` are contract documents that continuo-contracts already accepted as
    a Python contract; none is changed. The ontology file is read first, and
    the library's ``ontology`` rule runs over it alone. Each document then goes
    through ``normalise_python`` as the contract of ``project`` that reads
    nothing the ontology tracks, then through ``ontology.resolve``, and the
    rules ``contract``, ``coverage``, ``quality`` and ``privacy`` run over all
    of them. The findings come as the library returns them, sorted. A
    ``project`` the ontology does not declare gets the library's one
    information finding and one warning for each contract that writes a
    binding.

    Raises ``ContractError`` when ``ontology_path`` cannot be read or is not an
    ontology (see :func:`_read_ontology`), when the ``ontology`` rule finds an
    error in the file, whatever ``project`` is (the message is ``<file>: the
    ontology file has <n> error(s): run continuo-contracts ontology check
    <file>``, and no contract is read against the file), and when ``project``
    is blank. Findings of the ``ontology`` rule that are not errors do not stop
    the check.
    """
    loaded = _read_ontology(ontology_path)
    broken = sum(finding.severity == "error" for finding in ontology_findings(loaded))
    if broken:
        raise ContractError(
            f"{ontology_path}: the ontology file has {broken} error(s): "
            f"run continuo-contracts ontology check {ontology_path}"
        )
    try:
        stored = [normalise_python(doc, project=project, reads=[]) for doc in docs]
    except ContractsError as exc:
        raise ContractError(str(exc)) from None
    resolved = [ontology.resolve(contract, loaded) for contract in stored]
    return check(loaded, resolved, rules=PER_PROJECT_RULES)
