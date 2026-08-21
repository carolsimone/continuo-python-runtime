"""Contract v1 model."""

from dataclasses import dataclass, field
from typing import Any

# Module-level constants
CRITICALITIES = frozenset({"REGULATORY", "CORE", "SECONDARY"})
EXTRA_COLUMNS_POLICIES = frozenset({"raise", "warn"})
KINDS = frozenset({"python-model", "python-csv"})
CONTRACT_VERSION = 1


@dataclass(frozen=True)
class Column:
    """A column definition in a table."""

    name: str
    type: str
    nullable: bool = True


@dataclass(frozen=True)
class Node:
    """A node (task/process) definition."""

    schema: str
    table: str
    owner: str
    schedule: str
    criticality: str
    script: str
    reads: dict[str, str]
    output_columns: tuple[Column, ...]
    description: str = ""
    extra_columns: str = "raise"
    config: dict[str, Any] = field(default_factory=dict)
    content_hash: str | None = None
    kind: str = "python-model"

    @property
    def relation(self) -> str:
        """Return the fully qualified table name."""
        return f"{self.schema}.{self.table}"
