"""Contract v1 model."""

from dataclasses import dataclass, field
from typing import Any

# Module-level constants
CRITICALITIES = frozenset({"REGULATORY", "CORE", "SECONDARY"})
EXTRA_COLUMNS_POLICIES = frozenset({"raise", "warn"})
KINDS = frozenset({"python-node", "python-csv", "python-api"})
DEFAULT_KIND = "python-node"
# How the harness writes a node's table on each run: replace empties it first,
# append keeps the rows of earlier runs, upsert overwrites rows whose
# primary_key exists and keeps the rest.
MODE_TYPES = frozenset({"append", "upsert", "replace"})
DEFAULT_MODE_TYPE = "replace"
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
    kind: str = DEFAULT_KIND
    secret_ref: str = ""
    mode_type: str = DEFAULT_MODE_TYPE
    primary_key: tuple[str, ...] = ()

    @property
    def relation(self) -> str:
        """Return the fully qualified table name."""
        return f"{self.schema}.{self.table}"
