"""Per-kind reads rules: how the runtime checks the reads of a node of each kind.

continuo-contracts decides which fields a node of each kind must, may and may
not carry. The runtime keeps one rule per kind for itself, the check of its
reads: a python-node's reads each parse as one read query under the
repository's dialect and name the schema of every table they read, a
python-csv node's uri is one its readers fetch, and a python-api node reads
nothing. Adding a kind means adding one entry to ``RULES``; nothing that loads
a contract branches on the kind's name.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from continuo_engine_contract.sql import ensure_single_read  # type: ignore[import-untyped]
from sqlglot.errors import ParseError, TokenError

from continuo_python_runtime.contract.model import KINDS
from continuo_python_runtime.contract.read_tables import unqualified_tables
from continuo_python_runtime.csv_source import parse_csv_uri
from continuo_python_runtime.errors import ContractError


class ReadsRule(ABC):
    """Validates a node's ``reads`` mapping and returns it unchanged."""

    @abstractmethod
    def validate(
        self, reads: Any, label: str, *, dialect: str | None, check_reads: bool
    ) -> dict[str, str]: ...


class SqlReads(ReadsRule):
    """A non-empty mapping of read name -> single SELECT.

    When the reads are checked, every table a query names must name its schema
    (a CTE of the query excepted), as topology-controller requires of a release.
    """

    def validate(
        self, reads: Any, label: str, *, dialect: str | None, check_reads: bool
    ) -> dict[str, str]:
        if not isinstance(reads, dict) or not reads:
            raise ContractError(
                f"{label}: 'reads' must be a non-empty mapping of name -> SQL"
            )
        for name, sql in reads.items():
            if not isinstance(name, str) or not name.strip():
                raise ContractError(
                    f"{label}: 'reads' name {name!r} must be a non-empty string"
                )
            if not isinstance(sql, str) or not sql.strip():
                raise ContractError(
                    f"{label}: 'reads.{name}' must be a non-empty SQL string"
                )
            if not check_reads:
                continue
            try:
                ensure_single_read(sql, dialect)
            except (ValueError, TokenError) as exc:
                # ensure_single_read's own message is phrased for check_binds
                # (its only other caller today), so it's wrapped rather than
                # surfaced bare here. TokenError is also caught: an unterminated
                # string literal or comment fails sqlglot's tokenizer with a
                # TokenError, a SqlglotError sibling of ParseError and not a
                # subclass of ValueError -- despite ensure_single_read's
                # docstring promising every rejection is a ValueError. Only
                # TokenError, not the broader SqlglotError, is caught here: by
                # the time control reaches this point `dialect` has already
                # been validated once in load_contract_dir, so any other
                # SqlglotError a future sqlglot version might raise from this
                # call should surface as itself, not get relabeled as a
                # rejected read.
                raise ContractError(
                    f"{label}: 'reads.{name}' must be a single read query ({exc})"
                ) from exc
            try:
                unqualified = unqualified_tables(sql, dialect=dialect)
            except ParseError as exc:
                # ensure_single_read keeps the statements sqlglot parsed and
                # accepts a query that starts with an empty statement (`; select
                # ...`); sqlglot's parse_one, which finds the tables here and in
                # topology-controller, refuses it. The read is refused as the
                # release would refuse it.
                raise ContractError(
                    f"{label}: 'reads.{name}' must be a single read query ({exc})"
                ) from exc
            if unqualified:
                raise ContractError(
                    f"{label}: read {name!r} names table {unqualified[0]!r} without its schema; "
                    f"write <schema>.{unqualified[0]}"
                )
        return dict(reads)


class CsvRead(ReadsRule):
    """Exactly ``{csv: <uri>}``, the uri in the grammar ``parse_csv_uri`` accepts."""

    def validate(
        self, reads: Any, label: str, *, dialect: str | None, check_reads: bool
    ) -> dict[str, str]:
        if not isinstance(reads, dict) or set(reads) != {"csv"}:
            raise ContractError(
                f"{label}: a python-csv node's 'reads' must be exactly {{csv: <uri>}}"
            )
        try:
            parse_csv_uri(reads["csv"])
        except (ValueError, TypeError) as exc:
            raise ContractError(f"{label}: invalid csv uri: {exc}") from exc
        return dict(reads)


class NoReads(ReadsRule):
    """No declared reads: the node's script fetches its own data."""

    def validate(
        self, reads: Any, label: str, *, dialect: str | None, check_reads: bool
    ) -> dict[str, str]:
        if reads is None or (isinstance(reads, dict) and not reads):
            return {}
        raise ContractError(
            f"{label}: a python-api node declares no 'reads' (its script fetches its own data)"
        )


@dataclass(frozen=True)
class KindRules:
    """The rules the runtime keeps for a node of one kind."""

    reads: ReadsRule


RULES: dict[str, KindRules] = {
    "python-node": KindRules(reads=SqlReads()),
    "python-csv": KindRules(reads=CsvRead()),
    "python-api": KindRules(reads=NoReads()),
}


def rules_for(kind: object, label: str) -> KindRules:
    """Return the rules for ``kind``; raise ``ContractError`` for any other value."""
    if isinstance(kind, str) and kind in RULES:
        return RULES[kind]
    raise ContractError(f"{label}: 'kind' must be one of {sorted(KINDS)}, got {kind!r}")
