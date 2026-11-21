"""Check a Python service's contracts against the company's ontology file.

``continuo-runtime validate --service <name> --ontology <file>`` runs the
per-service rules of continuo-contracts over the contracts of one service:
each contract is stored as a release stores it, filled with what the
ontology knows and checked, so a team sees in its own CI what a release says
about its bindings. This is the only module of the runtime that
imports the library's ontology modules.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from continuo_contracts import ontology
from continuo_contracts.contract import normalise_python
from continuo_contracts.errors import ContractsError, OntologyError
from continuo_contracts.ontology_check import PER_SERVICE_RULES, Finding, check

from continuo_python_runtime.errors import ContractError


def _read_ontology(ontology_path: Path) -> ontology.Ontology:
    """Return the ontology the file ``ontology_path`` holds.

    Raises ``ContractError``, with the message ``<file>: <reason>``, for a file
    that cannot be read (missing, a folder, no permission), one that is not
    UTF-8 text and one the library does not accept as an ontology, whose
    reason is the library's own message.
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


def check_service(
    docs: Sequence[Mapping[str, Any]], *, service: str, ontology_path: Path
) -> list[Finding]:
    """Return the findings of the per-service rules over ``docs``, the contracts of ``service``.

    ``docs`` are contract documents that continuo-contracts already accepted as
    a Python contract; none is changed. Each goes through ``normalise_python``
    as the contract of ``service`` that reads nothing the ontology tracks, then
    through ``ontology.resolve``, and the rules ``contract``, ``coverage``,
    ``quality`` and ``privacy`` run over all of them. The findings come as the
    library returns them, sorted. A ``service`` the ontology does not declare
    gets the library's one information finding and one warning for each
    contract that writes a binding.

    Raises ``ContractError`` when ``ontology_path`` cannot be read or is not an
    ontology (see :func:`_read_ontology`), and when ``service`` is blank.
    """
    loaded = _read_ontology(ontology_path)
    try:
        stored = [normalise_python(doc, service=service, reads=[]) for doc in docs]
    except ContractsError as exc:
        raise ContractError(str(exc)) from None
    resolved = [ontology.resolve(contract, loaded) for contract in stored]
    return check(loaded, resolved, rules=PER_SERVICE_RULES)
