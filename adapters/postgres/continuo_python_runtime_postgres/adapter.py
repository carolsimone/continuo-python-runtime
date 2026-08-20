"""Postgres implementation of the WarehouseAdapter port.

One class covers both roles Continuo asks of an engine:

* **Validation DDL** — ``ensure_schema``/``drop_schema`` manage the candidate
  schema; empty builds go through ``CREATE TABLE … AS (…) WITH NO DATA``, empty
  clones through ``CTAS … WHERE 1=0``, and ``check_binds`` EXPLAINs a read
  without scanning data. Schema creation is serialized on a session advisory
  lock because parallel root validation nodes race on ``CREATE SCHEMA``.
* **Python-node data-plane I/O** — ``fetch`` executes one declared read and
  returns an Arrow table; ``ensure_table``/``load`` build and atomically
  replace a table's contents.

The connection runs with ``autocommit = True``: every method owns its
transaction explicitly rather than relying on psycopg2's implicit
per-connection transaction. A single-statement method (``drop_schema``,
``build_empty_from_sql``, ``clone_empty_from_prod``) needs nothing further —
under autocommit each statement commits as it runs. A method that must apply
several statements atomically (``build_empty_from_columns``, ``check_binds``,
``ensure_table``, ``load``) wraps them in its own explicit
``BEGIN``/``COMMIT``, with a ``ROLLBACK`` on the error path so a mid-sequence
failure leaves no partial change and no aborted transaction behind for the
next call on the same connection.
"""
import hashlib
import logging
import os

from typing import Any

import psycopg2  # type: ignore[import-untyped]
import pyarrow as pa  # type: ignore[import-untyped]

from continuo_engine_contract.config import ensure_known_keys  # type: ignore[import-untyped]
from continuo_engine_contract.port import WarehouseAdapter  # type: ignore[import-untyped]
from continuo_engine_contract.sql import ensure_single_read  # type: ignore[import-untyped]
from continuo_engine_contract.types import validate_column_type  # type: ignore[import-untyped]
from psycopg2 import errors as pg_errors  # type: ignore[import-untyped]
from psycopg2 import sql as pg_sql  # type: ignore[import-untyped]
from psycopg2.extras import execute_values  # type: ignore[import-untyped]

logger = logging.getLogger("continuo_python_runtime_postgres")

# The postgres physical-layout vocabulary, mirroring dbt-postgres's own `indexes`
# config so the graph reads a python node's layout the way it reads a dbt model's.
# This adapter is the sole owner and enforcer of the vocabulary — the contract
# loader validates `config` as shape only and stays engine-blind (see
# continuo_python_runtime/contract/loader.py).
_KNOWN_CONFIG_KEYS: tuple[str, ...] = ("indexes",)
_KNOWN_INDEX_KEYS: tuple[str, ...] = ("columns", "unique", "type", "name")
# Access methods that can back a plain CREATE INDEX. The chosen value is
# interpolated into DDL, so this allowlist is an injection guard as much as a
# spelling check — the same discipline types.validate_column_type applies to types.
_INDEX_TYPES: tuple[str, ...] = ("brin", "btree", "gin", "gist", "hash", "spgist")
# Structural restrictions postgres places on the allowlisted access methods.
# Both were confirmed against postgres:16: btree is the only one that can enforce
# uniqueness, and hash is the only one that refuses a multicolumn index (brin,
# gin, gist and spgist all accept several columns). Checking them here turns a
# raw psycopg2 error into a message naming the config key at fault.
_UNIQUE_INDEX_TYPE = "btree"
_SINGLE_COLUMN_INDEX_TYPES = frozenset({"hash"})
# Postgres's own default access method, spelled out so the emitted DDL always
# carries an explicit USING clause. `USING btree` and a bare CREATE INDEX build
# the identical index, so naming it changes nothing at the warehouse while
# keeping one code path for every access method.
_DEFAULT_INDEX_TYPE = "btree"
# NAMEDATALEN - 1. Postgres silently truncates longer identifiers, so any
# index name over this limit -- derived default or explicit -- must be
# truncated here too, or the emitted DDL's name would not match the name
# postgres actually stores.
_MAX_IDENTIFIER_BYTES = 63


def _truncate_explicit_identifier(name: str) -> str:
    """Truncate an explicit, author-given identifier to at most 63 bytes.

    Truncates on encoded UTF-8 bytes, not characters: postgres's limit is
    bytes, and a multibyte identifier would slip past a character-wise
    slice. This is a *plain* byte-for-byte cut -- no digest suffix -- so it
    matches postgres's own NAMEDATALEN truncation exactly: the normalized
    name this function returns is always what postgres will actually store,
    never something it silently truncates further behind our back.

    Unlike :func:`_index_name`'s derived defaults, an explicit ``name:`` is
    something the author chose, so this deliberately does NOT inject a
    disambiguating digest the way ``_index_name`` does. Two different
    explicit names that happen to truncate to the same 63-byte prefix
    collide here on purpose: the caller's duplicate-name check (over these
    already-truncated names) surfaces that as a rejection instead of the two
    entries silently colliding at the warehouse, where ``CREATE INDEX IF NOT
    EXISTS`` would skip every index but the first that resolves to the same
    stored identifier.
    """
    encoded = name.encode("utf-8")
    if len(encoded) <= _MAX_IDENTIFIER_BYTES:
        return name
    return encoded[:_MAX_IDENTIFIER_BYTES].decode("utf-8", "ignore")


def _index_name(table: str, columns: list[str]) -> str:
    """Return the default index name for *columns* on *table*, within 63 bytes.

    Truncates on encoded UTF-8 bytes, not characters: postgres's limit is
    bytes, and a multibyte identifier would slip past a character-wise slice.

    A name that overflows the limit keeps a uniqueness-preserving suffix --
    ``"_"`` plus the leading 8 hex characters of
    ``sha256(<full untruncated name>)`` -- rather than a bare truncation: a
    long enough table name leaves ``ix_<table>_`` alone at or past 63 bytes,
    so every index on it would otherwise truncate to the *same* default name,
    and ``CREATE INDEX IF NOT EXISTS`` would then silently skip every index
    after the first. The digest is taken over the full name, so two
    different column lists that truncate to the same prefix still get
    different suffixes. Unlike :func:`_truncate_explicit_identifier`, nobody
    chose this literal string, so injecting a digest to keep it collision-free
    is a service rather than a surprise.

    The name deliberately covers only the table and the column list, NOT the
    access method or uniqueness. It names indexes on PRODUCTION tables under
    ``CREATE INDEX IF NOT EXISTS``, so folding more of the definition into it
    would rename every index that already exists and build a second copy
    alongside each one. Two entries over the same columns therefore resolve to
    one name and are rejected as duplicates by :func:`_validated_indexes`; an
    author who genuinely wants two access methods over one column set gives
    one of them an explicit ``name:``.
    """
    name = f"ix_{table}_{'_'.join(columns)}"
    encoded = name.encode("utf-8")
    if len(encoded) <= _MAX_IDENTIFIER_BYTES:
        return name
    digest = hashlib.sha256(encoded).hexdigest()[:8]
    truncated = encoded[: _MAX_IDENTIFIER_BYTES - 9].decode("utf-8", "ignore")
    return f"{truncated}_{digest}"


def _validated_indexes(
    config: dict[str, Any] | None, table: str, column_names: list[str]
) -> list[dict[str, Any]]:
    """Validate *config* against the postgres 'indexes' vocabulary; return normalized entries.

    Every key — top level and per index entry — is checked before the caller
    emits a single statement, so a malformed config never leaves a half-built
    table behind (fail closed). A ``None`` *config* returns ``[]``. Each
    returned entry carries ``columns`` (list[str]), ``unique`` (bool), ``type``
    (str, the access method, defaulting to ``btree``), and ``name`` (str,
    defaulted via :func:`_index_name` when not given explicitly).

    Index columns are checked against the node's own declared output columns:
    an index on a column the node does not produce is an authoring error, and
    pre-checking also keeps every identifier that reaches DDL to a name the
    spec itself declared.

    The structural access-method restrictions (``_UNIQUE_INDEX_TYPE``,
    ``_SINGLE_COLUMN_INDEX_TYPES``) are checked for the error message rather
    than for safety — postgres rejects those combinations itself — so the
    author reads "index 'unique' requires type 'btree'" instead of a raw
    psycopg2 traceback.

    Raises:
        ValueError: Naming the offending key, for any of: an unrecognized
            top-level key; ``indexes`` not a list, or a non-mapping element;
            an unrecognized index key; ``columns`` missing, not a list, empty,
            or containing a non-string or an empty string; an index column not
            present in *column_names*; ``unique`` present and not a bool;
            ``type`` outside :data:`_INDEX_TYPES`, or structurally incompatible
            with ``unique``/several columns; ``name`` present and not a
            non-empty string; or two entries resolving to the same index name
            -- letting that through would silently drop every colliding index
            but the first under ``CREATE INDEX IF NOT EXISTS``. An explicit
            ``name`` over 63 bytes is truncated the same way postgres itself
            would truncate it (:func:`_truncate_explicit_identifier`) *before*
            this comparison runs, so two over-long explicit names that share
            only their first 63 bytes are caught here too, not just exact
            duplicates.
    """
    if config is None:
        return []
    ensure_known_keys(config, _KNOWN_CONFIG_KEYS, "postgres")

    # .get, not a bare subscript: this function's whole job is failing closed
    # with a named message, and `indexes` is only guaranteed present while
    # _KNOWN_CONFIG_KEYS has exactly one member. A second postgres key would
    # otherwise turn `{"newkey": ...}` into a bare KeyError right here.
    raw_indexes = config.get("indexes", [])
    if not isinstance(raw_indexes, list):
        raise ValueError(f"config 'indexes' must be a list, got {type(raw_indexes).__name__}")

    declared = set(column_names)
    normalized: list[dict[str, Any]] = []
    for entry in raw_indexes:
        ensure_known_keys(
            entry, _KNOWN_INDEX_KEYS, "postgres", where="config 'indexes' entry"
        )

        columns = entry.get("columns")
        if (
            not isinstance(columns, list)
            or not columns
            or not all(isinstance(c, str) and c for c in columns)
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

        index_type = entry.get("type", _DEFAULT_INDEX_TYPE)
        if index_type not in _INDEX_TYPES:
            raise ValueError(
                f"unsupported index 'type' {index_type!r}; supported: {', '.join(_INDEX_TYPES)}"
            )
        if unique and index_type != _UNIQUE_INDEX_TYPE:
            raise ValueError(
                f"index 'unique' requires type {_UNIQUE_INDEX_TYPE!r}; postgres cannot "
                f"enforce uniqueness with access method {index_type!r}"
            )
        if index_type in _SINGLE_COLUMN_INDEX_TYPES and len(columns) > 1:
            raise ValueError(
                f"index 'type' {index_type!r} indexes a single column; "
                f"got {len(columns)}: {', '.join(repr(c) for c in columns)}"
            )

        name = entry.get("name")
        if name is not None and (not isinstance(name, str) or not name):
            raise ValueError(f"index 'name' must be a non-empty string, got {name!r}")

        normalized.append({
            "columns": list(columns),
            "unique": unique,
            "type": index_type,
            "name": (
                _truncate_explicit_identifier(name)
                if name is not None
                else _index_name(table, columns)
            ),
        })

    # Reject before returning -- this is the fail-closed gate that runs
    # before any DDL is emitted, so a collision (explicit `name` vs. another
    # explicit `name`, or vs. a derived default) must surface here rather
    # than let CREATE INDEX IF NOT EXISTS silently skip every index but the
    # first that resolves to the same name.
    seen_names: set[str] = set()
    for index in normalized:
        index_name = index["name"]
        if index_name in seen_names:
            raise ValueError(f"duplicate index name: {index_name!r}")
        seen_names.add(index_name)

    return normalized


def _index_ddl(
    schema: str, table: str, index: dict[str, Any], *, if_not_exists: bool
) -> "pg_sql.Composed":
    """Build one ``CREATE [UNIQUE] INDEX`` statement for a validated *index*.

    Every identifier — index name, schema, table, access method and each
    column — goes through ``pg_sql.Identifier``; never raw interpolation. The
    ``UNIQUE`` keyword and the ``IF NOT EXISTS`` clause are constants from this
    module, and the access method comes from this module's own allowlist, not
    from author input.

    *if_not_exists* is True on the ``ensure_table`` path, which creates a table
    that may already exist and already carry its indexes, and False on the
    ``build_empty_from_columns`` path, which has just dropped and recreated the
    table so no index of that name can survive.
    """
    return pg_sql.SQL("CREATE {}INDEX {}{} ON {}.{} USING {} ({})").format(
        pg_sql.SQL("UNIQUE ") if index["unique"] else pg_sql.SQL(""),
        pg_sql.SQL("IF NOT EXISTS " if if_not_exists else ""),
        pg_sql.Identifier(index["name"]),
        pg_sql.Identifier(schema),
        pg_sql.Identifier(table),
        pg_sql.Identifier(index["type"]),
        pg_sql.SQL(", ").join(pg_sql.Identifier(c) for c in index["columns"]),
    )


def _column_ddl(columns: list[dict[str, Any]]) -> "pg_sql.Composed":
    """Compile declared typed *columns* into a CREATE TABLE column list.

    The single typed-DDL compiler behind both build paths — the validation
    rebuild (:meth:`PostgresAdapter.build_empty_from_columns`) and the runtime
    create (:meth:`PostgresAdapter.ensure_table`) — so the two cannot drift into
    emitting different shapes for the same declared columns. Callers must have
    run :func:`~continuo_engine_contract.types.validate_column_type` over every
    ``type`` first: the type text is interpolated as SQL, only the name is
    identifier-quoted. ``nullable`` absent means True, i.e. no constraint.
    """
    col_defs = []
    for col in columns:
        parts = [pg_sql.Identifier(col["name"]), pg_sql.SQL(col["type"])]
        if not col.get("nullable", True):
            parts.append(pg_sql.SQL("NOT NULL"))
        col_defs.append(pg_sql.SQL(" ").join(parts))
    return pg_sql.SQL(", ").join(col_defs)


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


class PostgresAdapter(WarehouseAdapter):
    """WarehouseAdapter speaking postgres over a psycopg2 connection."""

    def __init__(self, conn: "psycopg2.extensions.connection") -> None:
        self._conn = conn
        self._conn.autocommit = True

    @classmethod
    def required_env(cls) -> list[str]:
        """Vars that must be non-empty before connecting."""
        return ["POSTGRES_HOST", "POSTGRES_DB", "POSTGRES_USER"]

    @classmethod
    def from_env(cls) -> "PostgresAdapter":
        """Connect from POSTGRES_* env (port defaults 5432, password empty)."""
        conn = psycopg2.connect(
            host=os.environ["POSTGRES_HOST"],
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ.get("POSTGRES_PASSWORD", ""),
        )
        return cls(conn)

    # --- Schema lifecycle ---------------------------------------------------

    def ensure_schema(self, schema: str) -> None:
        """Idempotently create *schema*; safe under concurrent callers.

        Race-safe: root validation nodes dispatch in parallel and can collide
        on CREATE SCHEMA. Serialize on a session advisory lock keyed by schema
        name; tolerate DuplicateSchema/UniqueViolation as a second line of
        defense. The session advisory lock is held independently of any
        transaction, and under ``autocommit = True`` each statement here runs
        (and commits, or aborts on failure) as its own single-statement
        transaction, so no statement ever finds the connection sitting in a
        transaction left open by a previous call.

        The explicit ``self._conn.commit()``/``self._conn.rollback()`` calls
        below are therefore no-ops against the server — there is never an
        open multi-statement transaction for them to act on under autocommit
        — but they are kept as a defensive no-op rather than removed: they
        cost nothing, and they keep this method's shape identical whether or
        not a future caller ever constructs this adapter over a
        non-autocommit connection.
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

    def drop_schema(self, schema: str) -> None:
        """Idempotently drop *schema* and everything in it; no-op if absent."""
        with self._conn.cursor() as cur:
            stmt = pg_sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                pg_sql.Identifier(schema)
            )
            logger.info("dropping candidate schema %s", schema)
            cur.execute(stmt)

    # --- Validation builds --------------------------------------------------

    def build_empty_from_sql(self, schema: str, table: str, compiled_sql: str) -> None:
        """Create ``schema.table`` empty, shaped by the compiled SELECT."""
        # Strip any trailing terminator so the SELECT nests cleanly inside AS ( ... ).
        inner = compiled_sql.strip().rstrip(";").strip()
        with self._conn.cursor() as cur:
            cur.execute(
                pg_sql.SQL("DROP TABLE IF EXISTS {}.{}").format(
                    pg_sql.Identifier(schema), pg_sql.Identifier(table)
                )
            )
            cur.execute(
                pg_sql.SQL("CREATE TABLE {}.{} AS ({}) WITH NO DATA").format(
                    pg_sql.Identifier(schema),
                    pg_sql.Identifier(table),
                    pg_sql.SQL(inner),
                )
            )

    def clone_empty_from_prod(self, candidate_schema: str, prod_schema: str, table: str) -> None:
        """Create ``candidate_schema.table`` empty, shaped like ``prod_schema.table``."""
        with self._conn.cursor() as cur:
            cur.execute(
                pg_sql.SQL("DROP TABLE IF EXISTS {}.{}").format(
                    pg_sql.Identifier(candidate_schema), pg_sql.Identifier(table)
                )
            )
            cur.execute(
                pg_sql.SQL(
                    "CREATE TABLE {}.{} AS SELECT * FROM {}.{} WHERE 1=0"
                ).format(
                    pg_sql.Identifier(candidate_schema),
                    pg_sql.Identifier(table),
                    pg_sql.Identifier(prod_schema),
                    pg_sql.Identifier(table),
                )
            )

    def build_empty_from_columns(
        self, schema: str, table: str, columns: list[dict], config: dict
    ) -> None:
        """Create ``schema.table`` empty from declared typed columns (drop-then-create).

        *config* carries this engine's physical-layout vocabulary: ``indexes``,
        a list of ``{"columns": [...], "unique": bool, "type": str, "name":
        str}`` mirroring dbt-postgres's own config. Every key — top level and
        per entry — is validated before the first statement runs, and the
        rebuild itself runs in one transaction, so a bad block fails the
        release gate without leaving a half-built table behind. An empty
        *config* emits exactly the statements contract 0.4.0 emitted, plus the
        transaction around them.
        """
        for col in columns:
            validate_column_type(col["type"])
        col_ddl = _column_ddl(columns)
        indexes = _validated_indexes(config, table, [col["name"] for col in columns])
        with self._conn.cursor() as cur:
            # Postgres DDL is transactional, so the drop, the create and every
            # index either all land or none do. Without this, an index postgres
            # refuses — a missing operator class for the column's type, say,
            # which no allowlist here can predict — would leave the table
            # rebuilt and partially indexed. BEGIN it explicitly for the same
            # reason check_binds does.
            cur.execute("BEGIN")
            try:
                cur.execute(
                    pg_sql.SQL("DROP TABLE IF EXISTS {}.{}").format(
                        pg_sql.Identifier(schema), pg_sql.Identifier(table)
                    )
                )
                cur.execute(
                    pg_sql.SQL("CREATE TABLE {}.{} ({})").format(
                        pg_sql.Identifier(schema),
                        pg_sql.Identifier(table),
                        col_ddl,
                    )
                )
                for index in indexes:
                    logger.info(
                        "creating %sindex on %s.%s (%s) using %s",
                        "unique " if index["unique"] else "",
                        schema, table, ", ".join(index["columns"]), index["type"],
                    )
                    cur.execute(_index_ddl(schema, table, index, if_not_exists=False))
            except Exception:
                # The failure itself is what the caller must see, so a rollback
                # that also fails only logs. Skipping it would strand the
                # session in an aborted transaction and fail every later
                # statement in the same Job on a poisoned connection.
                try:
                    cur.execute("ROLLBACK")
                except psycopg2.Error as rollback_exc:
                    logger.error("build rollback failed: %s", rollback_exc)
                raise
            cur.execute("COMMIT")

    def check_binds(self, sql: str) -> None:
        """Verify *sql* binds against current schema state; EXPLAIN scans no data.

        *sql* must be a single read query, and that is decided by parsing it —
        see :func:`continuo_engine_contract.sql.ensure_single_read` — before
        any of it reaches the server. Three defences, in order: the parse gate,
        an explicit read-only transaction, and the subquery wrap.
        """
        # Layer 1. psycopg2 sends a parameterless statement over the
        # simple-query protocol, which executes EVERY ``;``-separated statement
        # in the batch. The wrap below is not enough on its own: a read that
        # closes the wrap's paren and reopens it after an injected statement
        # (``select 1) AS x; DELETE FROM t; SELECT * FROM (SELECT 1``) leaves
        # the wrapped text balanced, and the DELETE runs for real. So decide the
        # shape by parsing first.
        ensure_single_read(sql, dialect="postgres")
        inner = sql.strip().rstrip(";").strip()
        with self._conn.cursor() as cur:
            logger.info("bind-checking read via EXPLAIN")
            # Layer 2. Backstop: even if the gate is ever bypassed, the engine
            # itself refuses to mutate.
            cur.execute("BEGIN READ ONLY")
            try:
                # Layer 3. A ``;`` inside the parentheses is a syntax error, so a
                # naively stacked statement cannot survive even the server's own
                # parse, and a non-query statement is not a legal subquery — do
                # not "simplify" this back to `EXPLAIN {inner}`.
                cur.execute(
                    pg_sql.SQL("EXPLAIN SELECT * FROM (\n{}\n) AS __check_binds__").format(
                        pg_sql.SQL(inner)
                    )
                )
            finally:
                # Must run on the error path too: a failed EXPLAIN leaves the
                # session in an aborted transaction, and without this every
                # later read in the same Job would fail on a poisoned
                # connection. A ROLLBACK failure only logs — the EXPLAIN's own
                # error, if any, is the one worth propagating.
                try:
                    cur.execute("ROLLBACK")
                except psycopg2.Error as rollback_exc:
                    logger.error("bind-check rollback failed: %s", rollback_exc)

    # --- Python-node data plane ---------------------------------------------

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

    @classmethod
    def validate_config(
        cls, config: dict[str, Any] | None, column_names: list[str]
    ) -> None:
        """Validate *config* against this engine's vocabulary, without connecting.

        The harness calls this immediately after selecting the node, so a
        malformed ``config`` — a singular ``index:`` typo, an index on an
        undeclared column — fails in the first second of the run instead of
        after the script has already computed its whole result. It is a
        tripwire, not the enforcement point: ``ensure_table`` runs the very
        same check again, unchanged, and remains the thing that guarantees no
        DDL is emitted for a bad config. Both go through
        :func:`_validated_indexes`, so the two cannot drift apart.

        The index name derived here is thrown away, so a placeholder table
        name is passed: nothing about naming is being validated (any string
        is accepted), and no name reaches an error message.

        Like ``config`` on ``ensure_table``, this method is not declared by
        the abstract ``WarehouseAdapter`` in ``continuo-engine-contract``;
        this repo ships both adapters and the harness as one coordinated
        release. The harness skips the call for an adapter that does not
        provide it (see ``docs/boundary-contract.md`` §13.4), which costs that
        adapter only earliness, never enforcement.

        Raises:
            ValueError: Exactly as :func:`_validated_indexes` does.
        """
        _validated_indexes(config, "_", column_names)

    def ensure_table(
        self,
        schema: str,
        table: str,
        columns: list[dict[str, Any]],
        *,
        config: dict[str, Any],
    ) -> None:
        """CREATE TABLE IF NOT EXISTS with typed DDL compiled from *columns*.

        Each column dict carries ``name``, ``type`` (validated against the
        contract's SQL type grammar), ``nullable`` (bool). *config* carries
        this engine's physical-layout vocabulary — ``indexes`` — validated by
        :func:`_validated_indexes` before any DDL runs, so a malformed config
        never leaves a half-built table behind (fail closed). Required and
        keyword-only, matching the contract 0.6.0 port signature. The create
        and every index either all land or none do, in the same explicit
        BEGIN/COMMIT/ROLLBACK shape build_empty_from_columns uses.
        """
        indexes = _validated_indexes(config, table, [col["name"] for col in columns])
        for col in columns:
            validate_column_type(col["type"])

        self.ensure_schema(schema)

        stmt = pg_sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} ({})").format(
            pg_sql.Identifier(schema),
            pg_sql.Identifier(table),
            _column_ddl(columns),
        )
        with self._conn.cursor() as cur:
            cur.execute("BEGIN")
            try:
                logger.info("ensuring table %s.%s exists", schema, table)
                cur.execute(stmt)
                for index in indexes:
                    logger.info(
                        "ensuring index %s on %s.%s exists", index["name"], schema, table
                    )
                    cur.execute(_index_ddl(schema, table, index, if_not_exists=True))
                cur.execute("COMMIT")
            except Exception:
                try:
                    cur.execute("ROLLBACK")
                except psycopg2.Error as rollback_exc:
                    logger.error("ensure_table rollback failed: %s", rollback_exc)
                raise

    def load(self, schema: str, table: str, data: "pa.Table") -> None:
        """Atomically replace ``schema.table``'s contents with *data*.

        One explicit transaction: TRUNCATE then batched inserts
        (``execute_values``, page_size 1000) in the Arrow table's column
        order; commits on success, rolls back and re-raises on any error. A
        0-row *data* is just a TRUNCATE. The transaction is opened explicitly
        (``BEGIN``) rather than relying on an implicit one, matching every
        other multi-statement method on this class under ``autocommit = True``.
        """
        columns = data.schema.names
        with self._conn.cursor() as cur:
            cur.execute("BEGIN")
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
                cur.execute("COMMIT")
            except Exception:
                try:
                    cur.execute("ROLLBACK")
                except psycopg2.Error as rollback_exc:
                    logger.error("load rollback failed: %s", rollback_exc)
                raise

    def close(self) -> None:
        """Release the underlying connection."""
        self._conn.close()
