"""Typed column definitions, validated against the contract's SQL type grammar."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from continuo_engine_contract.types import validate_column_type  # type: ignore[import-untyped]

from .identifiers import Identifier

_TEMPORAL = re.compile(r"^(TIMESTAMP|DATE)\Z", re.IGNORECASE | re.ASCII)


def is_temporal_type(type_str: str) -> bool:
    """True for the contract types that support year/month/day/hour partitioning."""
    return _TEMPORAL.match(type_str) is not None


@dataclass(frozen=True)
class ColumnDefinition:
    """One declared output column. ``type`` is validated on construction because
    the text is interpolated into DDL; the name is quoted by the renderer."""

    name: Identifier
    type: str
    nullable: bool = True

    def __post_init__(self) -> None:
        validate_column_type(self.type)

    @property
    def is_temporal(self) -> bool:
        return is_temporal_type(self.type)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ColumnDefinition":
        if not isinstance(raw, Mapping) or "name" not in raw or "type" not in raw:
            raise ValueError(f"column must be a mapping with 'name' and 'type', got {raw!r}")
        return cls(Identifier(raw["name"]), raw["type"], bool(raw.get("nullable", True)))


def column_types(columns: Sequence[ColumnDefinition]) -> dict[str, str]:
    """Declared column name -> contract type, for layout validation."""
    return {column.name.name: column.type for column in columns}
