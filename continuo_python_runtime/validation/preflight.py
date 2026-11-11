"""The ``seed_preflight`` and ``probe_views`` validation ops.

``seed_preflight`` asks the warehouse which objects a reload of a seed's
production table would destroy or leave reading a missing table, and compares
them with the objects the operator has allowed or acknowledged. ``check`` stops
a reload that would hit an object outside that set; ``report`` prints the same
conclusion and never stops. ``probe_views`` tries each named view with a
zero-row read and reports which ones no longer bind, and never fails the op for
it.

Each op prints exactly one structured result block as the last line on stdout
(``continuo_engine_contract.result``) and returns the process exit code: 0 when
the op ran to its conclusion (or ``report`` mode), 1 when ``check`` stops, 2
when the op could not run (a bad input, no adapter, missing adapter env, an
unexpected error). The block's ``status`` is ``error`` for exit codes 1 and 2.
Diagnostics go to stderr through ``logging``; stdout carries only the block.

The decision of ``seed_preflight`` is the pure function :func:`decide`; the rest
of the module reads the inputs, drives the adapter and prints.
"""
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

from continuo_engine_contract import result  # type: ignore[import-untyped]
from continuo_engine_contract.port import (  # type: ignore[import-untyped]
    AdapterDiscoveryError,
    DependentsIntrospection,
    WarehouseAdapter,
    discover_adapter,
)

logger = logging.getLogger("validation_preflight")

_Discover = Callable[[], tuple[str, type[WarehouseAdapter]]]

# The adapter's ``drop_semantics`` mapped to the engine family the release plan declares.
_FAMILY_OF_DROP_SEMANTICS = {"cascade": "schema_bound", "late_binding": "late_binding"}
_MODES = ("check", "report")
_FAMILIES = ("schema_bound", "late_binding")

# Longest driver text kept in a block; the first line only, so a block stays small.
_MAX_TEXT = 500


class Situation(Enum):
    """How far ``seed_preflight`` got, which is what decides its verdict."""

    BAD_INPUT = "bad_input"
    ENGINE_MISMATCH = "engine_mismatch"
    NO_CAPABILITY = "no_capability"
    UNREACHABLE = "unreachable"
    LISTING_FAILED = "listing_failed"
    LISTED = "listed"


@dataclass(frozen=True)
class Verdict:
    """What ``seed_preflight`` concludes: the block's status, reason, message and the exit code."""

    status: str
    reason: str
    exit_code: int
    checked: bool
    completeness: str
    message: str


def decide(
    mode: str,
    family: str,
    situation: Situation,
    *,
    detail: str = "",
    completeness: str = "none",
    has_unowned: bool = False,
) -> Verdict:
    """The decision table of ``seed_preflight``.

    *family* is the engine family the release expects (``schema_bound`` or
    ``late_binding``), *detail* the text that explains the situation, *completeness*
    the adapter's own and *has_unowned* whether a listed object is outside the
    allowed set. A situation that a ``check`` stops on gives ``error`` and exit 1;
    ``report`` mode gives the same ``reason`` on a ``success`` block and exit 0. A
    bad input is exit 2 in both modes. A ``reason`` prefixes the message.
    """
    if situation is Situation.BAD_INPUT:
        return Verdict("error", "", 2, False, "none", detail)
    reason, stops = _conclusion(family, situation, has_unowned)
    failed = stops and mode == "check"
    return Verdict(
        status="error" if failed else "success",
        reason=reason,
        exit_code=1 if failed else 0,
        checked=situation is Situation.LISTED,
        completeness=completeness if situation is Situation.LISTED else "none",
        message=f"{reason}: {detail}" if reason else detail,
    )


def _conclusion(family: str, situation: Situation, has_unowned: bool) -> tuple[str, bool]:
    """The ``reason`` a situation carries and whether a ``check`` stops on it."""
    schema_bound = family == "schema_bound"
    if situation is Situation.ENGINE_MISMATCH:
        return "engine_mismatch", True
    if situation is Situation.UNREACHABLE:
        return "warehouse_unreachable", True
    if situation is Situation.NO_CAPABILITY:
        return ("unowned_dependents", True) if schema_bound else ("", False)
    if situation is Situation.LISTING_FAILED:
        return "", schema_bound
    if schema_bound and has_unowned:
        return "unowned_dependents", True
    return "", False


class _InputError(Exception):
    """An input of the op is missing or malformed."""


def _first_line(exc: BaseException) -> str:
    """The first line of an exception's text, at most ``_MAX_TEXT`` characters, never empty."""
    text = str(exc).strip()
    line = text.splitlines()[0] if text else ""
    return (line or type(exc).__name__)[:_MAX_TEXT]


def _json_list(env: Mapping[str, str], name: str, *, required: bool) -> list[str]:
    """The env var *name* as a JSON array of strings; unset or empty is ``[]`` unless *required*."""
    raw = env.get(name, "")
    if not raw.strip():
        if required:
            raise _InputError(f"missing required env var {name}")
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _InputError(f"{name} is not valid JSON: {exc}") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _InputError(f"{name} must be a JSON array of strings")
    return cast(list[str], value)


def _emit(
    status: str, message: str, unique_id: str, reason: str, fields: dict[str, Any]
) -> None:
    """Print the op's one result block, the last line on stdout."""
    print(
        result.result_block(
            status, message, 1 if status == "error" else 0, unique_id, reason=reason, fields=fields
        ),
        flush=True,
    )


def _discover_and_check_env(
    env: Mapping[str, str], discover: _Discover
) -> tuple[str, type[WarehouseAdapter]]:
    """The installed adapter, after checking its required env is present in *env*."""
    try:
        engine, adapter_cls = discover()
    except AdapterDiscoveryError as exc:
        raise _InputError(str(exc)) from exc
    missing = [name for name in adapter_cls.required_env() if not env.get(name)]
    if missing:
        raise _InputError(f"missing required env for engine {engine!r}: {', '.join(missing)}")
    return engine, adapter_cls


def _close(adapter: Any) -> None:
    """Close the adapter; a failure only logs, so it never masks the op's outcome."""
    try:
        adapter.close()
    except Exception as exc:
        logger.error("adapter close failed: %s", exc)


# --- seed_preflight ---------------------------------------------------------------


@dataclass(frozen=True)
class _SeedInputs:
    mode: str
    family: str
    schema: str
    table: str
    allowed: frozenset[str]


def _seed_node_id(env: Mapping[str, str]) -> str:
    """``NODE_ID``, else ``<PROD_SCHEMA>.<TABLE_NAME>`` when both are set, else ``""``."""
    schema, table = env.get("PROD_SCHEMA", ""), env.get("TABLE_NAME", "")
    return env.get("NODE_ID") or (f"{schema}.{table}" if schema and table else "")


def _seed_inputs(env: Mapping[str, str]) -> _SeedInputs:
    mode = env.get("SEED_PREFLIGHT_MODE", "")
    if mode not in _MODES:
        raise _InputError(f"SEED_PREFLIGHT_MODE must be one of {', '.join(_MODES)}, got {mode!r}")
    family = env.get("SEED_PREFLIGHT_ENGINE_FAMILY", "")
    if family not in _FAMILIES:
        raise _InputError(
            f"SEED_PREFLIGHT_ENGINE_FAMILY must be one of {', '.join(_FAMILIES)}, got {family!r}"
        )
    for name in ("PROD_SCHEMA", "TABLE_NAME"):
        if not env.get(name):
            raise _InputError(f"missing required env var {name}")
    allowed = _json_list(env, "SEED_PREFLIGHT_ALLOWED", required=False)
    acknowledged = _json_list(env, "SEED_PREFLIGHT_ACKNOWLEDGED", required=False)
    return _SeedInputs(
        mode=mode,
        family=family,
        schema=env["PROD_SCHEMA"],
        table=env["TABLE_NAME"],
        allowed=frozenset(entry.strip().lower() for entry in [*allowed, *acknowledged]),
    )


def _seed_fields(
    env: Mapping[str, str], drop_semantics: str, verdict: Verdict, dependents: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "op": "seed_preflight",
        "mode": env.get("SEED_PREFLIGHT_MODE", ""),
        "checked": verdict.checked,
        "engine_family": env.get("SEED_PREFLIGHT_ENGINE_FAMILY", ""),
        "drop_semantics": drop_semantics,
        "completeness": verdict.completeness,
        "dependents": dependents,
    }


def _finish_seed(
    env: Mapping[str, str], drop_semantics: str, verdict: Verdict,
    dependents: list[dict[str, Any]] | None = None,
) -> int:
    node_id = _seed_node_id(env)
    if verdict.exit_code == 2:
        logger.error("seed_preflight %s: %s", node_id, verdict.message)
    elif verdict.reason or verdict.exit_code:
        logger.warning("seed_preflight %s: %s", node_id, verdict.message)
    _emit(
        verdict.status, verdict.message, node_id, verdict.reason,
        _seed_fields(env, drop_semantics, verdict, dependents or []),
    )
    return verdict.exit_code


def run_seed_preflight(
    env: Mapping[str, str], discover: _Discover = discover_adapter
) -> int:
    """Run ``seed_preflight`` against *env*; print its one block and return the exit code.

    Reads the op's inputs and the adapter's ``required_env()`` from *env* (adapters
    connect from ``os.environ``, so the runner passes that). An unexpected error
    still prints one ``error`` block and returns 2.
    """
    try:
        return _seed_preflight(env, discover)
    except Exception as exc:
        logger.exception("seed_preflight %s failed unexpectedly", _seed_node_id(env))
        verdict = Verdict("error", "", 2, False, "none", f"unexpected error: {_first_line(exc)}")
        return _finish_seed(env, "", verdict)


def _seed_preflight(env: Mapping[str, str], discover: _Discover) -> int:
    try:
        inputs = _seed_inputs(env)
        engine, adapter_cls = _discover_and_check_env(env, discover)
    except _InputError as exc:
        return _finish_seed(env, "", decide("", "", Situation.BAD_INPUT, detail=str(exc)))

    declared = getattr(adapter_cls, "drop_semantics", "")
    drop_semantics = declared if isinstance(declared, str) else ""
    adapter_family = _FAMILY_OF_DROP_SEMANTICS.get(drop_semantics)

    def finish(
        situation: Situation, detail: str = "", *, completeness: str = "none",
        has_unowned: bool = False, dependents: list[dict[str, Any]] | None = None,
    ) -> int:
        verdict = decide(
            inputs.mode, inputs.family, situation,
            detail=detail, completeness=completeness, has_unowned=has_unowned,
        )
        return _finish_seed(env, drop_semantics, verdict, dependents)

    if adapter_family != inputs.family:
        return finish(
            Situation.ENGINE_MISMATCH,
            f"engine {engine!r} is {adapter_family or 'of no known family'}, "
            f"but the release expects {inputs.family}",
        )
    if not issubclass(adapter_cls, DependentsIntrospection):
        return finish(Situation.NO_CAPABILITY, f"engine {engine!r} cannot list the objects that read a table")

    try:
        adapter = adapter_cls.from_env()
    except Exception as exc:
        return finish(Situation.UNREACHABLE, _first_line(exc))
    try:
        try:
            listing = cast(DependentsIntrospection, adapter).dependents(inputs.schema, inputs.table)
        except Exception as exc:
            return finish(
                Situation.LISTING_FAILED,
                f"listing the objects that read {inputs.schema}.{inputs.table} failed: {_first_line(exc)}",
            )
        dependents = [
            {
                "schema": found.schema, "name": found.name, "kind": found.kind, "owner": found.owner,
                "depth": found.depth,
                "allowed": f"{found.schema}.{found.name}".lower() in inputs.allowed,
            }
            for found in sorted(listing.objects, key=lambda d: (d.depth, d.schema, d.name))
        ]
    finally:
        _close(adapter)
    outside = [d for d in dependents if not d["allowed"]]
    if inputs.family == "schema_bound":
        detail = "the reload would drop objects continuo does not own: " + ", ".join(
            f"{d['schema']}.{d['name']} ({d['kind']}, owner {d['owner']})" for d in outside
        )
    else:
        detail = (
            f"{len(outside)} external view(s) may go stale; recreate them after the reload: "
            + ", ".join(f"{d['schema']}.{d['name']}" for d in outside)
        )
    return finish(
        Situation.LISTED, detail if outside else "",
        completeness=listing.completeness, has_unowned=bool(outside), dependents=dependents,
    )


# --- probe_views ------------------------------------------------------------------


def _probe_views_input(env: Mapping[str, str]) -> list[tuple[str, str]]:
    """``PROBE_VIEWS`` as ``(schema, name)`` pairs, split on the first dot, each once, in order."""
    pairs: list[tuple[str, str]] = []
    for entry in _json_list(env, "PROBE_VIEWS", required=True):
        schema, dot, name = entry.partition(".")
        if not dot or not schema or not name:
            raise _InputError(f"PROBE_VIEWS entry {entry!r} is not <schema>.<name>")
        if (schema, name) not in pairs:
            pairs.append((schema, name))
    return pairs


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _finish_probe(
    env: Mapping[str, str], status: str, message: str, reason: str, checked: bool,
    views: list[dict[str, Any]],
) -> int:
    node_id = env.get("NODE_ID") or "probe_views"
    if status == "error":
        logger.error("probe_views %s: %s", node_id, message)
    elif reason:
        logger.warning("probe_views %s: %s", node_id, message)
    _emit(status, message, node_id, reason, {"op": "probe_views", "checked": checked, "views": views})
    return 2 if status == "error" else 0


def run_probe_views(env: Mapping[str, str], discover: _Discover = discover_adapter) -> int:
    """Run ``probe_views`` against *env*; print its one block and return the exit code.

    Reads ``PROBE_VIEWS`` and the adapter's ``required_env()`` from *env*. A view
    that fails its probe, or a warehouse that cannot be reached, still ends with
    exit 0; only an input or adapter problem or an unexpected error is exit 2.
    """
    try:
        return _probe_views(env, discover)
    except Exception as exc:
        logger.exception("probe_views failed unexpectedly")
        return _finish_probe(env, "error", f"unexpected error: {_first_line(exc)}", "", False, [])


def _probe_views(env: Mapping[str, str], discover: _Discover) -> int:
    try:
        pairs = _probe_views_input(env)
        _, adapter_cls = _discover_and_check_env(env, discover)
    except _InputError as exc:
        return _finish_probe(env, "error", str(exc), "", False, [])
    try:
        adapter = adapter_cls.from_env()
    except Exception as exc:
        return _finish_probe(
            env, "success", f"warehouse_unreachable: {_first_line(exc)}", "warehouse_unreachable", False, []
        )
    views: list[dict[str, Any]] = []
    try:
        for schema, name in pairs:
            error = ""
            try:
                adapter.fetch(f"SELECT * FROM {_quote(schema)}.{_quote(name)} LIMIT 0")
            except Exception as exc:
                error = _first_line(exc)
            views.append({"schema": schema, "name": name, "ok": not error, "error": error})
    finally:
        _close(adapter)
    broken = [f"{v['schema']}.{v['name']}" for v in views if not v["ok"]]
    for name in broken:
        logger.warning("probe_views: %s does not bind", name)
    message = f"{len(broken)} of {len(views)} views failed: {', '.join(broken)}" if broken else ""
    return _finish_probe(env, "success", message, "", True, views)
