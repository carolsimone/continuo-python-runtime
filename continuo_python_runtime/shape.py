"""Compare a live table's columns with a node's declared output columns.

The harness runs this before every append or upsert: those modes keep earlier
rows, so writing into a table whose shape drifted from the contract would mix
two shapes in one table. Names are compared case-insensitively (Trino folds
names, DuckDB matches them case-insensitively) and nullability exactly. Order
is ignored, because every insert names its columns; types are ignored,
because engines report them in their own spelling (Trino and DuckDB drop a
VARCHAR's length) and there is no engine-to-contract type mapping. Type
changes are the release gate's job.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple

from continuo_engine_contract.port import LiveColumn  # type: ignore[import-untyped]

from continuo_python_runtime.contract.model import Column


class NullabilityDiff(NamedTuple):
    """A column whose nullability differs: as declared vs as the live table has it."""

    name: str
    declared: bool
    live: bool


def _yaml_bool(value: bool) -> str:
    return "true" if value else "false"


@dataclass(frozen=True)
class ShapeDiff:
    """What separates a live table from the declared columns; falsy when nothing does."""

    missing: tuple[str, ...] = ()
    unexpected: tuple[str, ...] = ()
    nullability: tuple[NullabilityDiff, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.missing or self.unexpected or self.nullability)

    def message(self, target: str) -> str:
        """One line naming every difference, for a LoadError about *target* (``schema.table``)."""
        parts: list[str] = []
        if self.missing:
            parts.append(f"missing column(s) {list(self.missing)}")
        if self.unexpected:
            parts.append(f"unexpected column(s) {list(self.unexpected)}")
        parts.extend(
            f"column {diff.name} is nullable: {_yaml_bool(diff.live)} in the table "
            f"but nullable: {_yaml_bool(diff.declared)} in the contract"
            for diff in self.nullability
        )
        return (
            f"table {target} does not match the contract's output_columns: {'; '.join(parts)}. "
            "An append or upsert table keeps its rows, so its shape cannot change: "
            "write to a new table (a _v2 name) under a new contract"
        )


def shape_diff(
    contract_columns: Sequence[Column], live_columns: Sequence[LiveColumn]
) -> ShapeDiff:
    """Compare the declared columns with a live table's.

    Each declared column is paired with a live column of the exact same name
    if there is one, else with one whose name matches case-insensitively, so
    two live columns that differ only in case pair with the right declared
    column.
    """
    remaining = list(live_columns)
    missing: list[str] = []
    nullability: list[NullabilityDiff] = []
    for declared in contract_columns:
        live = next((column for column in remaining if column.name == declared.name), None)
        if live is None:
            folded = declared.name.casefold()
            live = next(
                (column for column in remaining if column.name.casefold() == folded), None
            )
        if live is None:
            missing.append(declared.name)
            continue
        remaining.remove(live)
        if live.nullable != declared.nullable:
            nullability.append(NullabilityDiff(declared.name, declared.nullable, live.nullable))
    return ShapeDiff(
        missing=tuple(missing),
        unexpected=tuple(column.name for column in remaining),
        nullability=tuple(nullability),
    )
