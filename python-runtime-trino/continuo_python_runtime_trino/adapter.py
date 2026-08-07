"""Trino implementation of the RuntimeAdapter port.

Data-plane I/O for python nodes: ``fetch`` executes one declared read and returns
an Arrow table; ``ensure_table``/``load`` build and replace a table's contents.

Trino/Iceberg has no multi-statement transactions, so ``load`` cannot be a single
atomic TRUNCATE+INSERT the way the postgres adapter's is. Two atomic-replace
primitives were verified live against Trino 483 + the Iceberg REST catalog before
choosing one:

- ``CREATE OR REPLACE TABLE t AS SELECT * FROM stage`` is a single statement (one
  Iceberg metadata commit), but it **silently drops NOT NULL constraints** on the
  replaced table: replacing a table declared ``id BIGINT NOT NULL`` leaves the
  post-replace column nullable, and a subsequent ``ALTER TABLE ... ALTER COLUMN
  ... SET NOT NULL`` is not supported by this connector/version (syntax error).
  Verified live: an INSERT of NULL into the "NOT NULL" column succeeds after a
  CREATE OR REPLACE. Using this primitive would silently weaken every table's
  contract-typed nullability on its first ``load()`` after ``ensure_table()``.
- ``CREATE TABLE stage (LIKE target INCLUDING PROPERTIES) WITH (location =
  <fresh sibling>)`` + populate + two private-name renames preserves the target's
  exact columns, NOT NULL constraints, partitioning, format, and other exposed
  Iceberg properties. The explicit fresh location overrides the copied target
  location, avoiding Iceberg's "non-empty location" rejection. Verified live:
  nullability and table properties survive the swap.

``load`` therefore uses the RENAME-based swap. Its exact atomicity guarantee,
verified against the live stack: **each individual ALTER TABLE RENAME is one
atomic Iceberg metadata commit, but the two-statement swap as a whole is not**.
Nothing under `<schema>.<table>` is touched until every row has been inserted into
the staging table, so a failure before the swap begins leaves the prior contents
of the target completely untouched (verified with a NOT-NULL-violating row: the
insert into staging fails and the target table is unchanged). Once the swap
begins there is a brief window, between the two RENAME statements, during which
the target name refers to neither table; if the process dies in that window the
data survives under the logged private old-table name and requires manual
recovery. If the *second* rename raises (rather than the process dying), ``load``
makes a best-effort attempt to rename that private old relation back before
re-raising. A staging relation created by this load is dropped best-effort in
``finally``. If recovery fails, the private old relation is deliberately retained
because it contains the original target data.

``load`` assumes a single writer per table: Continuo's scheduler runs at most
one Job per node at a time. Concurrent ``load()`` calls against the same table
are unsupported because their target renames would race. Swap relations use a
per-load UUID, so distinct targets cannot collide with each other's temporary
names or with legitimate user tables.

The contract's SQL-type grammar (``continuo_validation_contract.types``, shared
with the postgres adapter) admits type spellings Trino does not recognize as
type names (``TEXT``, ``DOUBLE PRECISION``, ``NUMERIC(p,s)``); ``_trino_type``
maps those three to Trino's own spellings (``VARCHAR``, ``DOUBLE``,
``DECIMAL(p,s)``) after the grammar guard has already rejected anything
injection-shaped. Every other grammar token (``BIGINT``, ``INT``, ``INTEGER``,
``BOOLEAN``, ``TIMESTAMP``, ``DATE``, ``VARCHAR(n)``, ``CHAR(n)``,
``DECIMAL(p,s)``) is valid Trino DDL unchanged (verified live).
"""
import logging
import os
import uuid

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pyarrow as pa  # type: ignore[import-untyped]

from continuo_validation_contract.port import RuntimeAdapter  # type: ignore[import-untyped]
from continuo_validation_contract.types import validate_column_type  # type: ignore[import-untyped]

import trino

from trino.auth import BasicAuthentication

logger = logging.getLogger("continuo_python_runtime_trino")

# Grammar spellings that are not valid Trino type names, mapped to the Trino
# spelling with equivalent semantics. Matching is case-insensitive; lookup keys
# are uppercase.
_TRINO_TYPE_ALIASES = {
    "TEXT": "VARCHAR",
    "DOUBLE PRECISION": "DOUBLE",
}


def _trino_type(type_str: str) -> str:
    """Map a validated contract type string to its Trino DDL spelling.

    Must only be called after :func:`~continuo_validation_contract.types.
    validate_column_type` has accepted *type_str*. ``NUMERIC(p,s)`` maps to
    ``DECIMAL(p,s)``; ``TEXT`` and ``DOUBLE PRECISION`` map via
    ``_TRINO_TYPE_ALIASES``; everything else in the grammar is already valid
    Trino DDL and passes through unchanged.
    """
    upper = type_str.upper()
    if upper.startswith("NUMERIC("):
        return "DECIMAL(" + upper[len("NUMERIC("):]
    return _TRINO_TYPE_ALIASES.get(upper, type_str)


def _quote(identifier: str) -> str:
    """Double-quote a non-empty Trino identifier, escaping embedded quotes."""
    if not identifier:
        raise ValueError("identifier must not be empty")
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def _sql_string(value: str) -> str:
    """Return *value* as one single-quoted Trino string literal."""
    return "'" + value.replace("'", "''") + "'"


# The Iceberg connector's own physical-layout property names — not the Hive
# connector's `partitioned_by`. This adapter targets Iceberg, so `partitioning`
# is correct here; this is a deliberate decision of record, not something to
# "correct" toward the Hive spelling. This tuple's order is the emission
# order, so the DDL does not depend on the config mapping's own key order.
_KNOWN_CONFIG_KEYS: tuple[str, ...] = ("partitioning", "sorted_by", "format")
_ALLOWED_FORMATS = frozenset({"PARQUET", "ORC", "AVRO"})


def _table_properties(config: dict[str, Any] | None) -> str:
    """Validate *config* against the Iceberg vocabulary; render its ``WITH (...)`` clause.

    Returns a leading-space ``WITH (...)`` string, or ``""`` when *config* is
    absent, empty, or (impossible after validation) carries no recognized key.
    Partition transforms like ``day(event_ts)`` are expressions Trino parses
    out of a string literal, so every value is rendered through
    :func:`_sql_string` rather than identifier-quoted.

    Raises:
        ValueError: Naming the offending key, for any of: an unrecognized
            config key; ``partitioning``/``sorted_by`` not a non-empty list of
            non-empty strings; ``format`` not a string, or not one of
            ``PARQUET``/``ORC``/``AVRO`` after ``.upper()``.
    """
    if not config:
        return ""
    for key in config:
        if key not in _KNOWN_CONFIG_KEYS:
            raise ValueError(f"unrecognized config key: {key!r}")

    rendered = []
    for key in ("partitioning", "sorted_by"):
        if key not in config:
            continue
        value = config[key]
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(v, str) and v for v in value)
        ):
            raise ValueError(
                f"config {key!r} must be a non-empty list of non-empty strings, got {value!r}"
            )
        rendered.append(f"{key} = ARRAY[{', '.join(_sql_string(v) for v in value)}]")

    if "format" in config:
        value = config["format"]
        if not isinstance(value, str) or value.upper() not in _ALLOWED_FORMATS:
            raise ValueError(
                f"config 'format' must be one of {sorted(_ALLOWED_FORMATS)}, got {value!r}"
            )
        rendered.append(f"format = {_sql_string(value.upper())}")

    if not rendered:
        return ""
    return " WITH (" + ", ".join(rendered) + ")"


def _sibling_location(location: str, name: str) -> str:
    """Return a URI beside *location* whose final path component is *name*."""
    if not location:
        raise ValueError("table location must not be empty")

    parsed = urlsplit(location)
    path = parsed.path.rstrip("/")
    parent, separator, _ = path.rpartition("/")
    if separator:
        sibling_path = f"{parent}/{name}"
    elif parsed.netloc or parsed.path.startswith("/"):
        sibling_path = f"/{name}"
    else:
        sibling_path = name
    return urlunsplit(
        (parsed.scheme, parsed.netloc, sibling_path, parsed.query, parsed.fragment)
    )


def _arrow_table_from_rows(colnames: list[str], rows: list[Any]) -> "pa.Table":
    """Build a column-wise Arrow table from cursor description names and fetched rows.

    Type inference is left to pyarrow over the Python values the trino client
    yields (Decimal -> decimal128, date -> date32, datetime -> timestamp, bool,
    int, float, str). An empty result produces a 0-row table whose columns are
    typed ``null`` (``pa.nulls(0)`` per column) rather than inferred — the script
    and ``conform()`` define the output shape, so this is acceptable.

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
    return pa.table({name: pa.array(list(values)) for name, values in zip(colnames, by_column)})


# Batch size for multi-row INSERT VALUES statements in load().
_INSERT_BATCH_SIZE = 500


class TrinoRuntimeAdapter(RuntimeAdapter):
    """RuntimeAdapter speaking Trino (Iceberg connector) over the trino DBAPI."""

    def __init__(self, conn: "trino.dbapi.Connection", catalog: str) -> None:
        self._conn = conn
        self._catalog = catalog

    @classmethod
    def required_env(cls) -> list[str]:
        """Vars that must be non-empty before connecting."""
        return ["TRINO_HOST", "TRINO_CATALOG"]

    @classmethod
    def from_env(cls) -> "TrinoRuntimeAdapter":
        """Connect from TRINO_* env (port 8080, user continuo, http; password optional).

        Mirrors ``continuo_validation_trino.adapter.TrinoAdapter.from_env`` exactly.
        """
        catalog = os.environ["TRINO_CATALOG"]
        user = os.environ.get("TRINO_USER", "continuo")
        http_scheme = os.environ.get("TRINO_HTTP_SCHEME", "http")
        password = os.environ.get("TRINO_PASSWORD", "")
        if password and http_scheme != "https":
            raise ValueError(
                "TRINO_PASSWORD is set but TRINO_HTTP_SCHEME is not 'https'; "
                "Trino refuses basic auth over plaintext"
            )
        conn = trino.dbapi.connect(
            host=os.environ["TRINO_HOST"],
            port=int(os.environ.get("TRINO_PORT", "8080")),
            user=user,
            catalog=catalog,
            http_scheme=http_scheme,
            auth=BasicAuthentication(user, password) if password else None,
        )
        return cls(conn, catalog)

    def _schema_ref(self, schema: str) -> str:
        return f"{_quote(self._catalog)}.{_quote(schema)}"

    def _table_ref(self, schema: str, table: str) -> str:
        return f"{self._schema_ref(schema)}.{_quote(table)}"

    def _table_location(self, schema: str, table: str) -> str:
        """Return the Iceberg storage location registered for a target table."""
        properties_ref = self._table_ref(schema, f"{table}$properties")
        rows = self._execute(
            f"SELECT value FROM {properties_ref} WHERE key = 'location'"
        )
        if len(rows) != 1 or not rows[0] or not isinstance(rows[0][0], str):
            raise RuntimeError(f"could not determine Iceberg location for {schema}.{table}")
        location = rows[0][0]
        if not location:
            raise RuntimeError(f"Iceberg location is empty for {schema}.{table}")
        return location

    def _execute(self, statement: str, params: "list[Any] | None" = None) -> list[Any]:
        """Run one statement to completion and return its rows.

        The trino DBAPI is lazy: execute() only starts the query, so the results
        must be consumed for DDL/DML to actually take effect.
        """
        cur = self._conn.cursor()
        try:
            if params is not None:
                cur.execute(statement, params)
            else:
                cur.execute(statement)
            return list(cur.fetchall())
        finally:
            cur.close()

    def _schema_exists(self, schema: str) -> bool:
        rows = self._execute(f"SHOW SCHEMAS FROM {_quote(self._catalog)}")
        return any(row[0] == schema for row in rows)

    def _ensure_schema(self, schema: str) -> None:
        """Idempotently create *schema*; safe under concurrent callers.

        Trino has no advisory locks, so ``IF NOT EXISTS`` does not close the race:
        a concurrent creator can win between Trino's existence check and the
        metastore write. The loser surfaces as a user error or — on the Iceberg
        REST catalog — as an INTERNAL_ERROR query failure, so the error's shape
        cannot be trusted; only the end state can. Re-raise only if the schema is
        genuinely absent (mirrors the validation trino adapter's ensure_schema).
        """
        logger.info("ensuring schema %s.%s exists", self._catalog, schema)
        try:
            self._execute(f"CREATE SCHEMA IF NOT EXISTS {self._schema_ref(schema)}")
        except trino.exceptions.TrinoQueryError:
            if not self._schema_exists(schema):
                raise
            logger.info("schema %s already exists (concurrent create); continuing", schema)

    def fetch(self, sql: str) -> "pa.Table":
        """Execute one declared read and return the result as an Arrow table."""
        cur = self._conn.cursor()
        try:
            cur.execute(sql)
            rows = cur.fetchall()
            colnames = [d.name for d in cur.description] if cur.description else []
            return _arrow_table_from_rows(colnames, rows)
        finally:
            cur.close()

    def ensure_table(
        self,
        schema: str,
        table: str,
        columns: list[dict[str, Any]],
        config: dict[str, Any] | None = None,
    ) -> None:
        """CREATE TABLE IF NOT EXISTS with typed DDL compiled from *columns*.

        Each column dict carries ``name``, ``type`` (validated against the
        contract's SQL type grammar, then mapped to its Trino spelling),
        ``nullable`` (bool). NOT NULL is supported by this connector/version and
        is emitted for ``nullable=False`` columns (verified live). *config*
        carries this engine's physical-layout vocabulary — the Iceberg
        connector's own table properties (``partitioning``, ``sorted_by``,
        ``format``) — validated by :func:`_table_properties` before any DDL
        runs, so a malformed config never leaves a half-built table behind
        (fail closed), then rendered into the CREATE TABLE's ``WITH`` clause.
        ``config`` is keyword-defaulted because the abstract
        ``RuntimeAdapter.ensure_table`` in the pinned contract version does
        not declare it; the harness passes it unconditionally regardless.
        """
        properties = _table_properties(config)
        for col in columns:
            validate_column_type(col["type"])

        self._ensure_schema(schema)

        col_defs = []
        for col in columns:
            parts = [_quote(col["name"]), _trino_type(col["type"])]
            if not col.get("nullable", True):
                parts.append("NOT NULL")
            col_defs.append(" ".join(parts))

        ref = self._table_ref(schema, table)
        logger.info("ensuring table %s exists", ref)
        self._execute(f"CREATE TABLE IF NOT EXISTS {ref} ({', '.join(col_defs)}){properties}")

    def _insert_batches(self, table_ref: str, columns: list[str], data: "pa.Table") -> None:
        """Insert *data*'s rows into *table_ref* in batches of ``_INSERT_BATCH_SIZE``."""
        rows = data.to_pylist()
        col_list = ", ".join(_quote(c) for c in columns)
        for start in range(0, len(rows), _INSERT_BATCH_SIZE):
            batch = rows[start:start + _INSERT_BATCH_SIZE]
            placeholders = ", ".join(
                "(" + ", ".join(["?"] * len(columns)) + ")" for _ in batch
            )
            params = [row[c] for row in batch for c in columns]
            self._execute(
                f"INSERT INTO {table_ref} ({col_list}) VALUES {placeholders}", params
            )

    def load(self, schema: str, table: str, data: "pa.Table") -> None:
        """Replace ``schema.table``'s contents with *data* via a staged swap.

        See the module docstring for the atomicity guarantee this provides and
        the alternative primitive (``CREATE OR REPLACE TABLE ... AS SELECT``)
        that was verified and rejected for silently dropping NOT NULL. Nothing
        under the target name is touched until every batch has been inserted
        into the staging table, so a failure before the swap begins (e.g. a
        NOT-NULL-violating row) leaves the prior target contents untouched.
        """
        columns = data.schema.names
        target_ref = self._table_ref(schema, table)
        load_token = uuid.uuid4().hex
        stage_table = f"__continuo_stage_{load_token}"
        old_table = f"__continuo_old_{load_token}"
        stage_ref = self._table_ref(schema, stage_table)
        old_ref = self._table_ref(schema, old_table)
        target_location = self._table_location(schema, table)
        stage_location = _sibling_location(target_location, stage_table)
        stage_created = False
        try:
            logger.info("creating staging table %s like %s", stage_ref, target_ref)
            self._execute(
                f"CREATE TABLE {stage_ref} "
                f"(LIKE {target_ref} INCLUDING PROPERTIES) "
                f"WITH (location = {_sql_string(stage_location)})"
            )
            stage_created = True
            if data.num_rows:
                logger.info("loading %d row(s) into staging table %s", data.num_rows, stage_ref)
                self._insert_batches(stage_ref, columns, data)

            logger.info("swapping %s into place for %s", stage_ref, target_ref)
            self._execute(f"ALTER TABLE {target_ref} RENAME TO {old_ref}")
            try:
                self._execute(f"ALTER TABLE {stage_ref} RENAME TO {target_ref}")
            except Exception:
                logger.exception(
                    "swap-in of %s failed after renaming %s away; attempting recovery",
                    stage_ref, target_ref,
                )
                try:
                    self._execute(f"ALTER TABLE {old_ref} RENAME TO {target_ref}")
                except Exception:
                    logger.exception(
                        "recovery rename of %s back to %s failed; manual recovery required",
                        old_ref, target_ref,
                    )
                raise
            else:
                stage_created = False
                self._execute(f"DROP TABLE {old_ref}")
        finally:
            if stage_created:
                try:
                    self._execute(f"DROP TABLE {stage_ref}")
                except Exception:
                    logger.warning("best-effort cleanup of staging table %s failed", stage_ref)

    def close(self) -> None:
        """Release the underlying connection."""
        self._conn.close()
