"""The warehouse use cases: one class for both roles the port serves.

Validation DDL (this task) and python-node data-plane I/O (next task) share one
connection, so they are one implementation, as in the postgres and trino
adapters. The class depends only on the LakeGateway port; ``required_env`` and
``from_env`` stay abstract and are supplied by the composition root
(``continuo_duckdb_adapter.adapter.DuckDBAdapter``).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from continuo_engine_contract.port import WarehouseAdapter  # type: ignore[import-untyped]
from continuo_engine_contract.sql import ensure_single_read  # type: ignore[import-untyped]

from ..domain.columns import ColumnDefinition, column_types
from ..domain.identifiers import Identifier, QualifiedTable
from ..domain.layout import TableLayout
from .ports import LakeConflictError, LakeGateway

if TYPE_CHECKING:  # pragma: no cover
    import pyarrow as pa  # type: ignore[import-untyped]

logger = logging.getLogger("continuo_duckdb_adapter")

# DuckLake has no advisory lock. Parallel validation nodes race on creation and
# the loser fails its COMMIT with a snapshot conflict; the retry then finds the
# object exists. Bounded so a genuine, persistent conflict still surfaces.
_CONFLICT_ATTEMPTS = 5
_CONFLICT_BACKOFF_SECONDS = 0.05


def _columns(raw: list[dict]) -> list[ColumnDefinition]:
    return [ColumnDefinition.from_mapping(entry) for entry in raw]


class LakeWarehouse(WarehouseAdapter):
    """WarehouseAdapter implemented over a LakeGateway."""

    def __init__(
        self, gateway: LakeGateway, *, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self._gateway = gateway
        self._sleep = sleep

    def _retrying(self, operation: Callable[[], None]) -> None:
        for attempt in range(1, _CONFLICT_ATTEMPTS + 1):
            try:
                operation()
                return
            except LakeConflictError:
                if attempt == _CONFLICT_ATTEMPTS:
                    raise
                logger.info("concurrent change conflicted (attempt %d); retrying", attempt)
                self._sleep(_CONFLICT_BACKOFF_SECONDS * attempt)

    # --- Schema lifecycle ---------------------------------------------------

    def ensure_schema(self, schema: str) -> None:
        """Idempotently create *schema*; safe under concurrent callers."""
        target = Identifier(schema)
        logger.info("ensuring schema %s exists", schema)
        self._retrying(lambda: self._gateway.create_schema_if_not_exists(target))

    def drop_schema(self, schema: str) -> None:
        """Idempotently drop *schema* and everything in it; no-op if absent."""
        logger.info("dropping candidate schema %s", schema)
        self._gateway.drop_schema_cascade(Identifier(schema))

    # --- Validation builds --------------------------------------------------

    def build_empty_from_sql(self, schema: str, table: str, compiled_sql: str) -> None:
        """Create ``schema.table`` empty, shaped by the compiled SELECT."""
        inner = compiled_sql.strip().rstrip(";").strip()
        target = QualifiedTable.of(schema, table)
        with self._gateway.transaction():
            self._gateway.drop_table_if_exists(target)
            self._gateway.create_empty_table_as(target, inner)

    def clone_empty_from_prod(self, candidate_schema: str, prod_schema: str, table: str) -> None:
        """Create ``candidate_schema.table`` empty, shaped like ``prod_schema.table``."""
        target = QualifiedTable.of(candidate_schema, table)
        with self._gateway.transaction():
            self._gateway.drop_table_if_exists(target)
            self._gateway.create_empty_clone(target, QualifiedTable.of(prod_schema, table))

    def build_empty_from_columns(
        self, schema: str, table: str, columns: list[dict], config: dict
    ) -> None:
        """Create ``schema.table`` empty from declared typed columns (drop-then-create).

        Types and layout are validated before the first statement; the rebuild is
        one transaction, so a failure leaves the prior table in place.
        """
        defs = _columns(columns)
        layout = TableLayout.from_config(config, column_types(defs))
        target = QualifiedTable.of(schema, table)
        with self._gateway.transaction():
            self._gateway.drop_table_if_exists(target)
            self._gateway.create_table(target, defs)
            if not layout.is_empty:
                self._gateway.apply_layout(target, layout)

    def check_binds(self, sql: str) -> None:
        """Verify *sql* binds against current schema state; scans no data.

        The parse gate runs first: DuckDB executes every ``;``-separated
        statement handed to it, so a stacked statement would run for real.
        """
        ensure_single_read(sql, dialect="duckdb")
        inner = sql.strip().rstrip(";").strip()
        logger.info("bind-checking read via EXPLAIN")
        self._gateway.explain_read(inner)

    # --- Python-node data plane ---------------------------------------------

    def fetch(self, sql: str) -> "pa.Table":
        """Execute one declared read and return the result as an Arrow table."""
        data = self._gateway.fetch_arrow(sql)
        seen: set[str] = set()
        duplicates: set[str] = set()
        for name in data.schema.names:
            if name in seen:
                duplicates.add(name)
            seen.add(name)
        if duplicates:
            raise ValueError(f"duplicate column name(s) in SELECT result: {sorted(duplicates)!r}")
        return data

    @classmethod
    def validate_config(cls, config: dict[str, Any] | None, column_names: list[str]) -> None:
        """Validate *config* against this engine's vocabulary, without connecting.

        The harness calls this right after selecting the node so a malformed
        ``config`` fails in the first second, not after the script has run. It
        only knows column names, so the DATE/TIMESTAMP check for time transforms
        is deferred; ``ensure_table`` runs the complete check and remains the
        enforcement point (the harness skips adapters without this method; see
        docs/boundary-contract.md section 13.4).
        """
        TableLayout.from_config(config, {name: None for name in column_names})

    def ensure_table(
        self,
        schema: str,
        table: str,
        columns: list[dict[str, Any]],
        *,
        config: dict[str, Any],
    ) -> None:
        """Create the table if absent; layout is applied only when this call creates it.

        Validation comes first, so a bad config or type emits no statement. The
        existence check and the create share one transaction; a concurrent
        creator makes one side fail with a conflict, and the retry then finds
        the table and returns.
        """
        defs = _columns(columns)
        layout = TableLayout.from_config(config, column_types(defs))
        self.ensure_schema(schema)
        target = QualifiedTable.of(schema, table)
        self._retrying(lambda: self._create_if_absent(target, defs, layout))

    def _create_if_absent(
        self, target: QualifiedTable, defs: list[ColumnDefinition], layout: TableLayout
    ) -> None:
        with self._gateway.transaction():
            if self._gateway.table_exists(target):
                return
            logger.info("creating table %s.%s", target.schema.name, target.table.name)
            self._gateway.create_table(target, defs)
            if not layout.is_empty:
                self._gateway.apply_layout(target, layout)

    def load(self, schema: str, table: str, data: "pa.Table") -> None:
        """Atomically replace the table's contents with *data* (one transaction)."""
        target = QualifiedTable.of(schema, table)
        with self._gateway.transaction():
            self._gateway.delete_all(target)
            if data.num_rows:
                logger.info("loading %d row(s) into %s.%s", data.num_rows, schema, table)
                self._gateway.insert_arrow(target, data)

    def close(self) -> None:
        """Release the underlying connection."""
        self._gateway.close()
