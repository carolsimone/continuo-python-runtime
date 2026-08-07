"""Postgres implementation of the RuntimeAdapter port.

Data-plane I/O for python nodes: ``fetch`` executes one declared read and returns an
Arrow table; ``ensure_table``/``load`` build and atomically replace a table's
contents. Unlike the validation adapter (autocommit DDL), this adapter runs with
``autocommit = False`` so ``load`` is transactional (TRUNCATE + inserts commit or
roll back together); ``fetch`` and ``ensure_table`` each commit (or roll back on
error) so no operation leaves a transaction open across calls.
"""
import logging
import os

from typing import Any

import psycopg2  # type: ignore[import-untyped]
import pyarrow as pa  # type: ignore[import-untyped]

from continuo_validation_contract.port import RuntimeAdapter  # type: ignore[import-untyped]
from continuo_validation_contract.types import validate_column_type  # type: ignore[import-untyped]
from psycopg2 import errors as pg_errors  # type: ignore[import-untyped]
from psycopg2 import sql as pg_sql  # type: ignore[import-untyped]
from psycopg2.extras import execute_values  # type: ignore[import-untyped]

logger = logging.getLogger("continuo_python_runtime_postgres")

# The postgres physical-layout vocabulary: `indexes` is the only recognized
# top-level config key. This adapter is the sole owner and enforcer of this
# vocabulary — the contract loader validates `config` as shape only and stays
# engine-blind (see continuo_python_runtime/contract/loader.py).
_KNOWN_CONFIG_KEYS: tuple[str, ...] = ("indexes",)
_KNOWN_INDEX_KEYS: tuple[str, ...] = ("columns", "unique", "name")
# NAMEDATALEN - 1. Postgres silently truncates longer identifiers, so a derived
# name over this limit must be truncated here too, or the emitted DDL's name
# would not match the name postgres actually stores.
_MAX_IDENTIFIER_BYTES = 63


def _index_name(table: str, columns: list[str]) -> str:
    """Return the default index name for *columns* on *table*, within 63 bytes.

    Truncates on encoded UTF-8 bytes, not characters: postgres's limit is
    bytes, and a multibyte identifier would slip past a character-wise slice.
    """
    name = f"ix_{table}_{'_'.join(columns)}"
    encoded = name.encode("utf-8")
    if len(encoded) <= _MAX_IDENTIFIER_BYTES:
        return name
    return encoded[:_MAX_IDENTIFIER_BYTES].decode("utf-8", "ignore")


def _validated_indexes(
    config: dict[str, Any] | None, table: str, column_names: list[str]
) -> list[dict[str, Any]]:
    """Validate *config* against the postgres 'indexes' vocabulary; return normalized entries.

    Every key — top level and per index entry — is checked before the caller
    emits a single statement, so a malformed config never leaves a half-built
    table behind (fail closed). An absent or empty *config* returns ``[]``.
    Each returned entry carries ``columns`` (list[str]), ``unique`` (bool), and
    ``name`` (str, defaulted via :func:`_index_name` when not given explicitly).

    Raises:
        ValueError: Naming the offending key, for any of: an unrecognized
            top-level key; ``indexes`` not a list, or a non-mapping element;
            an unrecognized index key; ``columns`` missing, not a list, empty,
            or containing a non-string; an index column not present in
            *column_names*; ``unique`` present and not a bool; ``name``
            present and not a non-empty string.
    """
    if not config:
        return []
    for key in config:
        if key not in _KNOWN_CONFIG_KEYS:
            raise ValueError(f"unrecognized config key: {key!r}")

    raw_indexes = config["indexes"]
    if not isinstance(raw_indexes, list):
        raise ValueError(f"config 'indexes' must be a list, got {type(raw_indexes).__name__}")

    declared = set(column_names)
    normalized: list[dict[str, Any]] = []
    for entry in raw_indexes:
        if not isinstance(entry, dict):
            raise ValueError(f"each 'indexes' entry must be a mapping, got {entry!r}")
        for key in entry:
            if key not in _KNOWN_INDEX_KEYS:
                raise ValueError(f"unrecognized index key: {key!r}")

        columns = entry.get("columns")
        if (
            not isinstance(columns, list)
            or not columns
            or not all(isinstance(c, str) for c in columns)
        ):
            raise ValueError(
                f"index 'columns' must be a non-empty list of column names, got {columns!r}"
            )
        missing = [c for c in columns if c not in declared]
        if missing:
            raise ValueError(
                f"index on undeclared column(s) {missing!r}; "
                f"declared columns: {sorted(declared)!r}"
            )

        unique = entry.get("unique", False)
        if not isinstance(unique, bool):
            raise ValueError(f"index 'unique' must be a boolean, got {unique!r}")

        name = entry.get("name")
        if name is not None and (not isinstance(name, str) or not name):
            raise ValueError(f"index 'name' must be a non-empty string, got {name!r}")

        normalized.append({
            "columns": list(columns),
            "unique": unique,
            "name": name if name is not None else _index_name(table, columns),
        })
    return normalized


def _index_ddl(schema: str, table: str, index: dict[str, Any]) -> "pg_sql.Composed":
    """Build one ``CREATE [UNIQUE] INDEX IF NOT EXISTS`` statement for a validated *index*.

    Every identifier — index name, schema, table, and each column — goes
    through ``pg_sql.Identifier``; never raw interpolation.
    """
    return pg_sql.SQL("CREATE {}INDEX IF NOT EXISTS {} ON {}.{} ({})").format(
        pg_sql.SQL("UNIQUE ") if index["unique"] else pg_sql.SQL(""),
        pg_sql.Identifier(index["name"]),
        pg_sql.Identifier(schema),
        pg_sql.Identifier(table),
        pg_sql.SQL(", ").join(pg_sql.Identifier(c) for c in index["columns"]),
    )


def _arrow_table_from_rows(colnames: list[str], rows: list[tuple[Any, ...]]) -> "pa.Table":
    """Build a column-wise Arrow table from cursor description names and fetched rows.

    Type inference is left to pyarrow over the Python values psycopg2 yields
    (Decimal -> decimal128, date -> date32, datetime -> timestamp, bool, int,
    float, str). An empty result produces a 0-row table whose columns are typed
    ``null`` (``pa.nulls(0)`` per column) rather than inferred — the script and
    ``conform()`` define the output shape, so this is acceptable.

    Raises
    ------
    ValueError
        If *colnames* contains duplicates (e.g. ``SELECT 1 AS id, 2 AS id``):
        building a dict column-wise would otherwise silently drop all but the
        last occurrence, corrupting the result instead of surfacing an error.
    """
    seen: set[str] = set()
    duplicates: set[str] = set()
    for name in colnames:
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    if duplicates:
        raise ValueError(
            f"duplicate column name(s) in SELECT result: {sorted(duplicates)!r}"
        )
    if not rows:
        return pa.table({name: pa.nulls(0) for name in colnames})
    by_column = list(zip(*rows))
    return pa.table({name: pa.array(values) for name, values in zip(colnames, by_column)})


class PostgresRuntimeAdapter(RuntimeAdapter):
    """RuntimeAdapter speaking postgres over a psycopg2 connection, transactionally."""

    def __init__(self, conn: "psycopg2.extensions.connection") -> None:
        self._conn = conn
        self._conn.autocommit = False

    @classmethod
    def required_env(cls) -> list[str]:
        """Vars that must be non-empty before connecting."""
        return ["POSTGRES_HOST", "POSTGRES_DB", "POSTGRES_USER"]

    @classmethod
    def from_env(cls) -> "PostgresRuntimeAdapter":
        """Connect from POSTGRES_* env (port defaults 5432, password empty)."""
        conn = psycopg2.connect(
            host=os.environ["POSTGRES_HOST"],
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ.get("POSTGRES_PASSWORD", ""),
        )
        return cls(conn)

    def fetch(self, sql: str) -> "pa.Table":
        """Execute one declared read and return the result as an Arrow table."""
        with self._conn.cursor() as cur:
            try:
                cur.execute(sql)
                colnames = [d.name for d in cur.description] if cur.description else []
                rows = list(cur.fetchall()) if cur.description else []
                table = _arrow_table_from_rows(colnames, rows)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return table

    def _ensure_schema(self, schema: str) -> None:
        """Idempotently create *schema*; safe under concurrent callers.

        Copies the validation adapter's ensure_schema pattern: serialize on a
        session advisory lock keyed by schema name, tolerate
        DuplicateSchema/UniqueViolation as a second line of defense. Adapted for
        explicit transactions (autocommit is off here): each step commits (or
        rolls back an aborted transaction) before the next, since the session
        advisory lock is held independently of transaction boundaries — a
        ROLLBACK clears an aborted transaction's state without releasing it.

        Any CREATE SCHEMA failure — not just DuplicateSchema/UniqueViolation —
        must roll back before the ``finally`` block's unlock runs: under
        ``autocommit = False`` an unhandled error leaves the transaction
        aborted, and issuing the unlock inside an aborted transaction raises
        ``InFailedSqlTransaction``, which would mask the real error *and* skip
        the unlock, leaking the session advisory lock and hanging every other
        caller waiting on it.
        """
        with self._conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (schema,))
            self._conn.commit()
            try:
                stmt = pg_sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    pg_sql.Identifier(schema)
                )
                logger.info("ensuring schema %s exists", schema)
                try:
                    cur.execute(stmt)
                    self._conn.commit()
                except (pg_errors.DuplicateSchema, pg_errors.UniqueViolation):
                    self._conn.rollback()
                    logger.info(
                        "schema %s already exists (concurrent create); continuing", schema
                    )
                except Exception:
                    self._conn.rollback()
                    raise
            finally:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (schema,))
                self._conn.commit()

    def ensure_table(
        self,
        schema: str,
        table: str,
        columns: list[dict[str, Any]],
        config: dict[str, Any] | None = None,
    ) -> None:
        """CREATE TABLE IF NOT EXISTS with typed DDL compiled from *columns*.

        Each column dict carries ``name``, ``type`` (validated against the
        contract's SQL type grammar), ``nullable`` (bool). *config* carries
        this engine's physical-layout vocabulary — ``indexes`` — validated by
        :func:`_validated_indexes` before any DDL runs, so a malformed config
        never leaves a half-built table behind (fail closed). ``config`` is
        keyword-defaulted because the abstract ``RuntimeAdapter.ensure_table``
        in the pinned contract version does not declare it; the harness passes
        it unconditionally regardless.
        """
        indexes = _validated_indexes(config, table, [col["name"] for col in columns])
        for col in columns:
            validate_column_type(col["type"])

        self._ensure_schema(schema)

        col_defs = []
        for col in columns:
            parts = [pg_sql.Identifier(col["name"]), pg_sql.SQL(col["type"])]
            if not col.get("nullable", True):
                parts.append(pg_sql.SQL("NOT NULL"))
            col_defs.append(pg_sql.SQL(" ").join(parts))

        stmt = pg_sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} ({})").format(
            pg_sql.Identifier(schema),
            pg_sql.Identifier(table),
            pg_sql.SQL(", ").join(col_defs),
        )
        with self._conn.cursor() as cur:
            try:
                logger.info("ensuring table %s.%s exists", schema, table)
                cur.execute(stmt)
                for index in indexes:
                    logger.info(
                        "ensuring index %s on %s.%s exists", index["name"], schema, table
                    )
                    cur.execute(_index_ddl(schema, table, index))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def load(self, schema: str, table: str, data: "pa.Table") -> None:
        """Atomically replace ``schema.table``'s contents with *data*.

        One transaction: TRUNCATE then batched inserts (``execute_values``,
        page_size 1000) in the Arrow table's column order; commits on success,
        rolls back and re-raises on any error. A 0-row *data* is just a TRUNCATE.
        """
        columns = data.schema.names
        with self._conn.cursor() as cur:
            try:
                cur.execute(
                    pg_sql.SQL("TRUNCATE {}.{}").format(
                        pg_sql.Identifier(schema), pg_sql.Identifier(table)
                    )
                )
                if data.num_rows:
                    rows = data.to_pylist()
                    values = [tuple(row[c] for c in columns) for row in rows]
                    insert_prefix = pg_sql.SQL("INSERT INTO {}.{} ({})").format(
                        pg_sql.Identifier(schema),
                        pg_sql.Identifier(table),
                        pg_sql.SQL(", ").join(pg_sql.Identifier(c) for c in columns),
                    )
                    # execute_values reparses every percent token after composable
                    # identifiers have rendered. Escape literal identifier percents,
                    # then append its one values-list expansion token unescaped.
                    insert_stmt = (
                        insert_prefix.as_string(cur).replace("%", "%%") + " VALUES %s"
                    )
                    logger.info(
                        "loading %d row(s) into %s.%s", data.num_rows, schema, table
                    )
                    execute_values(cur, insert_stmt, values, page_size=1000)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        """Release the underlying connection."""
        self._conn.close()
