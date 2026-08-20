"""Validation adapter port and entry-point discovery.

Validation builds each node as an EMPTY table in the candidate schema; the DDL is
engine-specific and lives behind the ValidationAdapter port. Engine packages register
a ``continuo_validation.adapters`` entry point; each runner image installs exactly
one, so discovery — not configuration — selects the adapter.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

ENTRY_POINT_GROUP = "continuo_validation.adapters"
RUNTIME_ENTRY_POINT_GROUP = "continuo_runtime.adapters"

if TYPE_CHECKING:  # pragma: no cover
    import pyarrow


class AdapterDiscoveryError(Exception):
    """Installed adapter plugins do not resolve to exactly one usable engine."""


class ValidationAdapter(ABC):
    """Port for engine-specific empty-table DDL during blue/green validation.

    stdout is a parsed channel: the runner prints one sentinel-framed result block
    (see ``result.py``) as its last stdout line. Adapters must log diagnostics via
    the stdlib ``logging`` module (captured from the pod log) rather than printing
    them, must never write to stdout, and must never emit the
    ``===CONTINUO_VALIDATION_RESULT_BEGIN/END===`` marker strings themselves.
    """

    @classmethod
    @abstractmethod
    def required_env(cls) -> list[str]:
        """Names of env vars that must be non-empty before connecting."""

    @classmethod
    @abstractmethod
    def from_env(cls) -> "ValidationAdapter":
        """Construct a connected adapter from environment variables."""

    @abstractmethod
    def ensure_schema(self, schema: str) -> None:
        """Idempotently create *schema*; safe under concurrent callers."""

    @abstractmethod
    def drop_schema(self, schema: str) -> None:
        """Idempotently drop *schema* and everything in it; no-op if absent.

        Symmetric teardown for :meth:`ensure_schema`. The executor schedules this
        as a one-shot engine-image op once a validation reaches its terminal result,
        so the control plane never connects to the warehouse itself. *schema* is
        opaque and mapped to the engine's own dialect.
        """

    @abstractmethod
    def build_empty_from_sql(self, schema: str, table: str, compiled_sql: str) -> None:
        """Create ``schema.table`` empty, shaped by the compiled SELECT."""

    @abstractmethod
    def clone_empty_from_prod(self, candidate_schema: str, prod_schema: str, table: str) -> None:
        """Create ``candidate_schema.table`` empty, shaped like ``prod_schema.table``."""

    @abstractmethod
    def build_empty_from_columns(
        self, schema: str, table: str, columns: list[dict], config: dict
    ) -> None:
        """Create ``schema.table`` empty from declared typed columns.

        Rebuild semantics (drop-then-create), matching the other build ops.
        Each column dict carries ``name`` (an identifier — implementations
        MUST quote it with their engine's identifier escaping, never
        interpolate it raw), ``type`` (a string from the contract's type
        grammar — see :mod:`continuo_engine_contract.types`;
        implementations MUST validate before interpolating into DDL), and
        optional ``nullable`` (default True). Bare typed CREATE TABLE is
        engine-portable by design — no dialect families.

        *config* is the node's physical-layout block — indexes, sort keys,
        partitioning — written in THIS engine's own vocabulary and applied to
        the created table (postgres: ``indexes``; trino: ``partitioning``,
        ``sorted_by``, ``format``, ``format_version``). It is deliberately not
        engine-namespaced, so implementations MUST fail closed on any key they
        do not recognize: call
        :func:`continuo_engine_contract.config.ensure_known_keys` with
        this engine's known-key set, and validate every value, BEFORE emitting
        any DDL — a malformed config must never leave a half-built table
        behind. Identifiers and property values taken from *config* MUST be
        escaped or allowlisted before interpolation, exactly as the column
        path already does. An empty *config* MUST emit the same bare
        ``CREATE TABLE`` as contract 0.4.0 — no trailing clause, no property
        list, no index statements.
        """

    @abstractmethod
    def check_binds(self, sql: str) -> None:
        """Verify *sql* binds against the current schema state without scanning data.

        EXPLAIN/prepare-class mechanism per engine. Raises on missing
        tables/columns or type errors; returns None when the query binds.

        *sql* is a single read query, and implementations MUST establish that
        by calling :func:`continuo_engine_contract.sql.ensure_single_read`
        with their engine's dialect **before** building anything to send.
        Wrapping the read in a subquery is not a substitute: a read that closes
        the wrap's own parenthesis and reopens it after an injected statement
        leaves the wrap balanced, and a driver that batches ``;``-separated
        statements then executes the injected statement for real.
        """

    @abstractmethod
    def close(self) -> None:
        """Release the underlying connection."""


# Backwards-compatibility alias for engine packages published outside this
# workspace against contract <= 0.2, which subclass this name instead of
# ValidationAdapter. The engine libraries in this repo subclass ValidationAdapter
# directly; this alias exists solely for external adapter packages pinned to
# older contract releases.
WarehouseAdapter = ValidationAdapter


class RuntimeAdapter(ABC):
    """Port for engine-specific data-plane I/O at python-node runtime.

    The harness is the only caller: scripts never see this surface. Same
    stdout discipline as ValidationAdapter — log to stderr, never print.
    """

    @classmethod
    @abstractmethod
    def required_env(cls) -> list[str]:
        """Names of env vars that must be non-empty before connecting."""

    @classmethod
    @abstractmethod
    def from_env(cls) -> "RuntimeAdapter":
        """Construct a connected adapter from environment variables."""

    @abstractmethod
    def fetch(self, sql: str) -> "pyarrow.Table":
        """Execute one declared read and return the result as Arrow."""

    @abstractmethod
    def ensure_table(
        self, schema: str, table: str, columns: list[dict], *, config: dict
    ) -> None:
        """CREATE TABLE IF NOT EXISTS with the typed DDL compiled from columns.

        Each column dict carries ``name`` (an identifier — implementations
        MUST quote it with their engine's identifier escaping, never
        interpolate it raw), ``type`` (a string from the contract's type
        grammar — see :mod:`continuo_engine_contract.types`;
        implementations MUST validate before interpolating into DDL), and
        ``nullable`` (bool).

        *config* is the node's physical-layout block in this engine's own
        vocabulary (e.g. ``indexes``), applied when the table is created;
        callers pass ``{}`` for none. It is keyword-only: the harness passes
        it as ``config=`` unconditionally, and a positional-friendly
        signature would let a caller regress to positional passing unnoticed.
        Implementations MUST fail closed on any config key they do not
        recognize, and MUST validate config values before interpolating
        anything into DDL — the same discipline as
        ``ValidationAdapter.build_empty_from_columns``.
        """

    @abstractmethod
    def load(self, schema: str, table: str, data: "pyarrow.Table") -> None:
        """Atomically replace the table's contents with *data*."""

    @abstractmethod
    def close(self) -> None:
        """Release the underlying connection."""


def _discover(group: str, kind: str, base: type) -> tuple[str, type]:
    """Shared logic for discovering and loading an adapter plugin."""
    eps = list(entry_points(group=group))
    if not eps:
        raise AdapterDiscoveryError(
            f"no {kind} adapter installed (entry-point group {group!r} is empty); "
            f"install exactly one engine package"
        )
    if len(eps) > 1:
        names = ", ".join(sorted(ep.name for ep in eps))
        raise AdapterDiscoveryError(
            f"multiple {kind} adapters installed ({names}); an image must install exactly one"
        )
    ep = eps[0]
    try:
        cls = ep.load()
    except Exception as exc:
        raise AdapterDiscoveryError(f"failed to load adapter entry point {ep.name!r}: {exc}") from exc
    if not (isinstance(cls, type) and issubclass(cls, base)):
        raise AdapterDiscoveryError(f"entry point {ep.name!r} does not resolve to a {base.__name__} subclass")
    return ep.name, cls


def discover_adapter() -> tuple[str, type[ValidationAdapter]]:
    """Return ``(engine_name, adapter_class)`` from the single installed plugin.

    Raises
    ------
    AdapterDiscoveryError
        If zero or multiple adapters are installed, or the entry point does not
        resolve to a ValidationAdapter subclass.
    """
    return _discover(ENTRY_POINT_GROUP, "validation", ValidationAdapter)


def discover_runtime_adapter() -> tuple[str, type[RuntimeAdapter]]:
    """Return ``(engine_name, adapter_class)`` from the single installed runtime plugin."""
    return _discover(RUNTIME_ENTRY_POINT_GROUP, "runtime", RuntimeAdapter)
