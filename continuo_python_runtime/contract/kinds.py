"""Per-kind contract rules: which fields a node of each kind must carry.

Each kind is one ``KindRules`` entry composed of a script policy and a reads
rule. Adding a kind means adding one entry to ``RULES``; nothing that parses a
contract branches on the kind's name.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from continuo_engine_contract.sql import ensure_single_read  # type: ignore[import-untyped]
from sqlglot.errors import TokenError

from continuo_python_runtime.contract.model import KINDS
from continuo_python_runtime.csv_source import parse_csv_uri
from continuo_python_runtime.errors import ContractError


class ReadsRule(ABC):
    """Validates a node's ``reads`` mapping and returns it unchanged."""

    @abstractmethod
    def validate(
        self, reads: Any, label: str, *, dialect: str | None, check_reads: bool
    ) -> dict[str, str]: ...


class SqlReads(ReadsRule):
    """A non-empty mapping of read name -> single SELECT."""

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


@dataclass(frozen=True)
class KindRules:
    """What a node of one kind must declare."""

    script_required: bool
    reads: ReadsRule


RULES: dict[str, KindRules] = {
    "python-node": KindRules(script_required=True, reads=SqlReads()),
    "python-csv": KindRules(script_required=False, reads=CsvRead()),
}


def rules_for(kind: object, label: str) -> KindRules:
    """Return the rules for ``kind``; raise ``ContractError`` for any other value."""
    if isinstance(kind, str) and kind in RULES:
        return RULES[kind]
    raise ContractError(f"{label}: 'kind' must be one of {sorted(KINDS)}, got {kind!r}")
