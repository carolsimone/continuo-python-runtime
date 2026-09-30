"""DuckLakeSession: the LakeGateway implementation over one DuckDB connection.

The local DuckDB instance is in-memory and holds nothing durable: the catalog
(Postgres) and the data (S3/MinIO Parquet) are the warehouse. Extensions are
LOADed first and only INSTALLed when missing, so an image that baked them in at
build time starts offline and as a non-root user.

SQL is never logged: the secret statement carries credentials. The catalog
password is kept out of the ATTACH conninfo in a private libpq passfile, and
every engine error is redacted on its way out of the session.
"""
from __future__ import annotations

import contextlib
import logging
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from typing import TYPE_CHECKING

import duckdb

from ..application.ports import LakeConflictError, LakeGateway
from ..domain.columns import ColumnDefinition
from ..domain.identifiers import Identifier, QualifiedTable
from ..domain.layout import TableLayout
from .ddl import (
    BEGIN,
    BEGIN_READ_ONLY,
    COMMIT,
    ROLLBACK,
    DdlRenderer,
    attach_statement,
    install_extension,
    load_extension,
    s3_secret_statement,
    secret_texts,
    set_extension_directory,
    set_temp_directory,
    use_catalog,
)
from .extensions import EXTENSIONS
from .passfile import Passfile, can_store
from .settings import DuckLakeSettings

if TYPE_CHECKING:  # pragma: no cover
    import pyarrow as pa  # type: ignore[import-untyped]

logger = logging.getLogger("continuo_duckdb_adapter")

CATALOG_ALIAS = "lake"

# DuckLake creates its metadata tables on the first ATTACH of a fresh catalog
# and does not guard that against a concurrent first ATTACH (several Jobs of one
# run starting together): all but one fail on the duplicate CREATE. The loser
# retries on a fresh connection and then finds the catalog initialised. Bounded,
# and only this failure class is retried.
_ATTACH_ATTEMPTS = 5
_ATTACH_BACKOFF_SECONDS = 0.2


def _is_first_attach_race(exc: duckdb.Error) -> bool:
    message = str(exc)
    return "Failed to initialize DuckLake" in message and (
        "duplicate" in message.lower() or "already exists" in message.lower()
    )


def _load_extension(con: "duckdb.DuckDBPyConnection", name: str) -> None:
    try:
        con.execute(load_extension(name))
    except duckdb.Error:
        logger.info("installing duckdb extension %s", name)
        con.execute(install_extension(name))
        con.execute(load_extension(name))


def _translate(exc: duckdb.Error, secrets: Sequence[str]) -> Exception:
    """*exc* with every spelling of *secrets* masked; type and conflict mapping kept.

    A ``TransactionException`` becomes a ``LakeConflictError``; any other engine
    error keeps its type, or falls back to ``duckdb.Error`` when its constructor
    does not take a single message.
    """
    message = str(exc)
    for secret in secrets:
        if secret:
            message = message.replace(secret, "***")
    if isinstance(exc, duckdb.TransactionException):
        return LakeConflictError(message)
    try:
        return type(exc)(message)
    except Exception:  # an exception type with a non-standard constructor
        return duckdb.Error(message)


def _redact(exc: BaseException, secrets: Sequence[str]) -> str:
    """The text of *exc* with *secrets* masked, for log lines."""
    return str(_translate(exc, secrets)) if isinstance(exc, duckdb.Error) else str(exc)


class DuckLakeSession(LakeGateway):
    """Every DuckDB error leaves this class through ``_guarded``: DuckDB echoes
    the catalog conninfo (and so the password) in connection errors, and the
    runner and harness write ``str(exc)`` to the result block and the pod logs."""

    def __init__(
        self,
        connection: "duckdb.DuckDBPyConnection",
        renderer: DdlRenderer,
        *,
        secrets: Sequence[str] = (),
        passfile: Passfile | None = None,
    ) -> None:
        self._con = connection
        self._renderer = renderer
        self._secrets = tuple(secrets)
        self._passfile = passfile

    @classmethod
    def connect(
        cls, settings: DuckLakeSettings, *, sleep: Callable[[float], None] = time.sleep
    ) -> "DuckLakeSession":
        secrets = secret_texts(settings)
        passfile = Passfile(settings.catalog_password) if can_store(settings.catalog_password) else None
        try:
            for attempt in range(1, _ATTACH_ATTEMPTS + 1):
                try:
                    con = cls._open(settings, passfile)
                except duckdb.Error as exc:
                    if attempt < _ATTACH_ATTEMPTS and _is_first_attach_race(exc):
                        logger.info("first attach of a fresh catalog raced (attempt %d); retrying", attempt)
                        sleep(_ATTACH_BACKOFF_SECONDS * attempt)
                        continue
                    raise _translate(exc, secrets) from None
                return cls(con, DdlRenderer(CATALOG_ALIAS), secrets=secrets, passfile=passfile)
        except BaseException:
            if passfile:
                passfile.close()
            raise
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _open(settings: DuckLakeSettings, passfile: Passfile | None) -> "duckdb.DuckDBPyConnection":
        """One fresh connection, extensions loaded, secret created, catalog attached."""
        con = duckdb.connect()
        try:
            if settings.extension_directory:
                con.execute(set_extension_directory(settings.extension_directory))
            if settings.temp_directory:
                # In-memory DuckDB spills here; its default is under the
                # (root-owned) working directory, which a non-root uid cannot use.
                con.execute(set_temp_directory(settings.temp_directory))
            for extension in EXTENSIONS:
                _load_extension(con, extension)
            if settings.uses_s3:
                con.execute(s3_secret_statement(settings))
            con.execute(attach_statement(settings, CATALOG_ALIAS, passfile.path if passfile else None))
            con.execute(use_catalog(CATALOG_ALIAS))
        except BaseException:
            con.close()
            raise
        return con

    # --- plumbing -----------------------------------------------------------

    @contextlib.contextmanager
    def _guarded(self) -> Iterator[None]:
        try:
            yield
        except duckdb.Error as exc:
            raise _translate(exc, self._secrets) from None

    def _run(self, sql: str, parameters: list | None = None) -> "duckdb.DuckDBPyConnection":
        with self._guarded():
            if parameters is None:
                return self._con.execute(sql)
            return self._con.execute(sql, parameters)

    def _rollback(self) -> None:
        try:
            self._con.execute(ROLLBACK)
        except duckdb.Error as exc:
            # Never mask the failure that got us here.
            logger.warning("rollback failed: %s", _redact(exc, self._secrets))

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        self._run(BEGIN)
        try:
            yield
        except BaseException:
            self._rollback()
            raise
        # Outside the try: a COMMIT that fails has already ended the transaction
        # in DuckDB, so there is nothing to roll back.
        self._run(COMMIT)

    # --- LakeGateway --------------------------------------------------------

    def create_schema_if_not_exists(self, schema: Identifier) -> None:
        self._run(self._renderer.create_schema_if_not_exists(schema))

    def drop_schema_cascade(self, schema: Identifier) -> None:
        self._run(self._renderer.drop_schema_cascade(schema))

    def table_exists(self, table: QualifiedTable) -> bool:
        with self._guarded():
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
        # EXPLAIN binds without scanning; the transaction is read-only (a backstop,
        # as in the postgres adapter) and always rolled back.
        self._run(BEGIN_READ_ONLY)
        try:
            self._run(self._renderer.explain_read(sql))
        finally:
            self._rollback()

    def fetch_arrow(self, sql: str) -> "pa.Table":
        with self._guarded():
            return self._run(sql).to_arrow_table()

    def delete_all(self, table: QualifiedTable) -> None:
        self._run(self._renderer.delete_all(table))

    def insert_arrow(self, table: QualifiedTable, data: "pa.Table") -> None:
        view = f"__continuo_load_{uuid.uuid4().hex}"
        with self._guarded():
            self._con.register(view, data)
        try:
            self._run(self._renderer.insert_select(table, data.schema.names, view))
        finally:
            with contextlib.suppress(duckdb.Error):
                self._con.unregister(view)

    def close(self) -> None:
        try:
            self._con.close()
        finally:
            if self._passfile:
                self._passfile.close()
