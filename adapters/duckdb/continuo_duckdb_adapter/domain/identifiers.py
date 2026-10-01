"""Identifier value objects.

They validate and carry names. Turning a name into quoted SQL is the job of the
infrastructure layer's ``DdlRenderer``, so this module stays engine-neutral.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Identifier:
    """A schema, table or column name, validated but never quoted."""

    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError(f"identifier must be a non-empty string, got {self.name!r}")
        if "\x00" in self.name:
            raise ValueError("identifier must not contain a NUL character")


@dataclass(frozen=True)
class QualifiedTable:
    """A table addressed by schema and name."""

    schema: Identifier
    table: Identifier

    @classmethod
    def of(cls, schema: str, table: str) -> "QualifiedTable":
        return cls(Identifier(schema), Identifier(table))
