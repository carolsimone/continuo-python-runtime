"""The port the use cases need from a DuckLake. Implemented by infrastructure."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING

from ..domain.columns import ColumnDefinition
from ..domain.identifiers import Identifier, QualifiedTable
from ..domain.layout import TableLayout

if TYPE_CHECKING:  # pragma: no cover
    import pyarrow  # type: ignore[import-untyped]


class LakeConflictError(Exception):
    """A concurrent DuckLake transaction won a snapshot conflict; safe to retry."""


class LakeGateway(ABC):
    """Everything the warehouse use cases ask of one DuckLake connection."""

    @abstractmethod
    def transaction(self) -> AbstractContextManager[None]:
        """BEGIN ... COMMIT; ROLLBACK (logged, never masking) if the body raises."""

    @abstractmethod
    def create_schema_if_not_exists(self, schema: Identifier) -> None: ...

    @abstractmethod
    def drop_schema_cascade(self, schema: Identifier) -> None:
        """Drop the schema and everything in it; a no-op when absent."""

    @abstractmethod
    def table_exists(self, table: QualifiedTable) -> bool: ...

    @abstractmethod
    def drop_table_if_exists(self, table: QualifiedTable) -> None: ...

    @abstractmethod
    def create_table(
        self,
        table: QualifiedTable,
        columns: Sequence[ColumnDefinition],
        *,
        if_not_exists: bool = False,
    ) -> None: ...

    @abstractmethod
    def create_empty_table_as(self, table: QualifiedTable, select_sql: str) -> None:
        """Create *table* empty, shaped by the SELECT (no terminator, single read)."""

    @abstractmethod
    def create_empty_clone(self, target: QualifiedTable, source: QualifiedTable) -> None:
        """Create *target* empty with *source*'s shape."""

    @abstractmethod
    def apply_layout(self, table: QualifiedTable, layout: TableLayout) -> None:
        """Apply partitioning and sort order to an existing table."""

    @abstractmethod
    def explain_read(self, sql: str) -> None:
        """Bind-check one read without scanning data; raise if it does not bind."""

    @abstractmethod
    def fetch_arrow(self, sql: str) -> "pyarrow.Table": ...

    @abstractmethod
    def delete_all(self, table: QualifiedTable) -> None: ...

    @abstractmethod
    def insert_arrow(self, table: QualifiedTable, data: "pyarrow.Table") -> None:
        """Append *data* in its own column order."""

    @abstractmethod
    def close(self) -> None: ...
