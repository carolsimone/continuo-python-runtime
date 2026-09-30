"""DuckLakeSession: the LakeGateway implementation over one DuckDB connection.

The local DuckDB instance is in-memory and holds nothing durable: the catalog
(Postgres) and the data (S3/MinIO Parquet) are the warehouse. Extensions are
LOADed first and only INSTALLed when missing, so an image that baked them in at
build time starts offline and as a non-root user.

SQL is never logged: the ATTACH and secret statements carry credentials.
"""
from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING

import duckdb

from ..application.ports import LakeConflictError, LakeGateway
from ..domain.columns import ColumnDefinition
from ..domain.identifiers import Identifier, QualifiedTable
from ..domain.layout import TableLayout
from .ddl import DdlRenderer, attach_statement, quote_identifier, s3_secret_statement, sql_literal
from .settings import DuckLakeSettings

if TYPE_CHECKING:  # pragma: no cover
    import pyarrow as pa  # type: ignore[import-untyped]

logger = logging.getLogger("continuo_duckdb_adapter")

CATALOG_ALIAS = "lake"
_EXTENSIONS = ("ducklake", "postgres", "httpfs")


def _load_extension(con: "duckdb.DuckDBPyConnection", name: str) -> None:
    try:
        con.execute(f"LOAD {name}")
    except duckdb.Error:
        logger.info("installing duckdb extension %s", name)
        con.execute(f"INSTALL {name}")
        con.execute(f"LOAD {name}")


class DuckLakeSession(LakeGateway):
    def __init__(self, connection: "duckdb.DuckDBPyConnection", renderer: DdlRenderer) -> None:
        self._con = connection
        self._renderer = renderer

    @classmethod
    def connect(cls, settings: DuckLakeSettings) -> "DuckLakeSession":
        con = duckdb.connect()
        try:
            if settings.extension_directory:
                con.execute(f"SET extension_directory = {sql_literal(settings.extension_directory)}")
            for extension in _EXTENSIONS:
                _load_extension(con, extension)
            if settings.uses_s3:
                con.execute(s3_secret_statement(settings))
            con.execute(attach_statement(settings, CATALOG_ALIAS))
            con.execute(f"USE {quote_identifier(CATALOG_ALIAS)}")
        except BaseException:
            con.close()
            raise
        return cls(con, DdlRenderer(CATALOG_ALIAS))

    # --- plumbing -----------------------------------------------------------

    def _run(self, sql: str, parameters: list | None = None) -> "duckdb.DuckDBPyConnection":
        try:
            if parameters is None:
                return self._con.execute(sql)
            return self._con.execute(sql, parameters)
        except duckdb.TransactionException as exc:
            raise LakeConflictError(str(exc)) from exc

    def _rollback(self) -> None:
        try:
            self._con.execute("ROLLBACK")
        except duckdb.Error as exc:
            # Never mask the failure that got us here.
            logger.warning("rollback failed: %s", exc)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        self._con.execute("BEGIN")
        try:
            yield
            self._run("COMMIT")
        except BaseException:
            self._rollback()
            raise

    # --- LakeGateway --------------------------------------------------------

    def create_schema_if_not_exists(self, schema: Identifier) -> None:
        self._run(self._renderer.create_schema_if_not_exists(schema))

    def drop_schema_cascade(self, schema: Identifier) -> None:
        self._run(self._renderer.drop_schema_cascade(schema))

    def table_exists(self, table: QualifiedTable) -> bool:
        row = self._run(
            DdlRenderer.TABLE_EXISTS_QUERY,
            [self._renderer.catalog_name, table.schema.name, table.table.name],
        ).fetchone()
        return bool(row and row[0])

    def drop_table_if_exists(self, table: QualifiedTable) -> None:
        self._run(self._renderer.drop_table_if_exists(table))

    def create_table(
        self,
        table: QualifiedTable,
        columns: Sequence[ColumnDefinition],
        *,
        if_not_exists: bool = False,
    ) -> None:
        self._run(self._renderer.create_table(table, columns, if_not_exists=if_not_exists))

    def create_empty_table_as(self, table: QualifiedTable, select_sql: str) -> None:
        self._run(self._renderer.create_empty_table_as(table, select_sql))

    def create_empty_clone(self, target: QualifiedTable, source: QualifiedTable) -> None:
        self._run(self._renderer.create_empty_clone(target, source))

    def apply_layout(self, table: QualifiedTable, layout: TableLayout) -> None:
        if layout.partition_keys:
            logger.info("partitioning %s.%s", table.schema.name, table.table.name)
            self._run(self._renderer.set_partitioned_by(table, layout.partition_keys))
        if layout.sort_keys:
            logger.info("sorting %s.%s", table.schema.name, table.table.name)
            self._run(self._renderer.set_sorted_by(table, layout.sort_keys))

    def explain_read(self, sql: str) -> None:
        # EXPLAIN binds without scanning; the transaction is always rolled back.
        self._con.execute("BEGIN")
        try:
            self._run(self._renderer.explain_read(sql))
        finally:
            self._rollback()

    def fetch_arrow(self, sql: str) -> "pa.Table":
        return self._run(sql).to_arrow_table()

    def delete_all(self, table: QualifiedTable) -> None:
        self._run(self._renderer.delete_all(table))

    def insert_arrow(self, table: QualifiedTable, data: "pa.Table") -> None:
        view = f"__continuo_load_{uuid.uuid4().hex}"
        self._con.register(view, data)
        try:
            self._run(self._renderer.insert_select(table, data.schema.names, view))
        finally:
            self._con.unregister(view)

    def close(self) -> None:
        self._con.close()
