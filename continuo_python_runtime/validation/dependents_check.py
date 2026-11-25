"""The ``dependents_check`` validation op.

``dependents_check`` asks the warehouse which objects a reload of a seed's
production table would destroy or leave reading a missing table, and compares
them with the objects the operator has allowed or acknowledged. ``check`` stops
a reload that would hit an object outside that set; ``report`` prints the same
conclusion and never stops (exit 0). The block and the exit codes follow
:mod:`~continuo_python_runtime.validation.warehouse_read`.

The decision is the pure function :func:`decide`; the rest of the module reads
the inputs, drives the adapter and prints.
"""
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

from continuo_engine_contract.port import (  # type: ignore[import-untyped]
    DependentsIntrospection,
    discover_adapter,
)

from continuo_python_runtime.validation.warehouse_read import (
    Discover,
    InputError,
    close_quietly,
    discover_and_check_env,
    emit,
    first_line,
    json_list,
)

logger = logging.getLogger("validation_dependents_check")

# The adapter's ``drop_semantics`` mapped to the engine family the release plan declares.
_FAMILY_OF_DROP_SEMANTICS = {"cascade": "schema_bound", "late_binding": "late_binding"}
_MODES = ("check", "report")
_FAMILIES = ("schema_bound", "late_binding")


class Situation(Enum):
    """How far ``dependents_check`` got, which is what decides its verdict."""

    BAD_INPUT = "bad_input"
    ENGINE_MISMATCH = "engine_mismatch"
    NO_CAPABILITY = "no_capability"
    UNREACHABLE = "unreachable"
    LISTING_FAILED = "listing_failed"
    LISTED = "listed"


@dataclass(frozen=True)
class Verdict:
    """What ``dependents_check`` concludes: the block's status, reason, message and the exit code."""

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
    """The decision table of ``dependents_check``.

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
    mode = env.get("DEPENDENTS_CHECK_MODE", "")
    if mode not in _MODES:
        raise InputError(f"DEPENDENTS_CHECK_MODE must be one of {', '.join(_MODES)}, got {mode!r}")
    family = env.get("DEPENDENTS_CHECK_ENGINE_FAMILY", "")
    if family not in _FAMILIES:
        raise InputError(
            f"DEPENDENTS_CHECK_ENGINE_FAMILY must be one of {', '.join(_FAMILIES)}, got {family!r}"
        )
    for name in ("PROD_SCHEMA", "TABLE_NAME"):
        if not env.get(name):
            raise InputError(f"missing required env var {name}")
    allowed = json_list(env, "DEPENDENTS_CHECK_ALLOWED", required=False)
    acknowledged = json_list(env, "DEPENDENTS_CHECK_ACKNOWLEDGED", required=False)
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
        "op": "dependents_check",
        "mode": env.get("DEPENDENTS_CHECK_MODE", ""),
        "checked": verdict.checked,
        "engine_family": env.get("DEPENDENTS_CHECK_ENGINE_FAMILY", ""),
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
        logger.error("dependents_check %s: %s", node_id, verdict.message)
    elif verdict.reason or verdict.exit_code:
        logger.warning("dependents_check %s: %s", node_id, verdict.message)
    emit(
        verdict.status, verdict.message, node_id, verdict.reason,
        _seed_fields(env, drop_semantics, verdict, dependents or []),
    )
    return verdict.exit_code


def run_dependents_check(
    env: Mapping[str, str], discover: Discover = discover_adapter
) -> int:
    """Run ``dependents_check`` against *env*; print its one block and return the exit code.

    Reads the op's inputs and the adapter's ``required_env()`` from *env* (adapters
    connect from ``os.environ``, so the runner passes that). An unexpected error
    still prints one ``error`` block and returns 2.
    """
    try:
        return _dependents_check(env, discover)
    except Exception as exc:
        logger.exception("dependents_check %s failed unexpectedly", _seed_node_id(env))
        verdict = Verdict("error", "", 2, False, "none", f"unexpected error: {first_line(exc)}")
        return _finish_seed(env, "", verdict)


def _dependents_check(env: Mapping[str, str], discover: Discover) -> int:
    try:
        inputs = _seed_inputs(env)
        engine, adapter_cls = discover_and_check_env(env, discover)
    except InputError as exc:
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
        return finish(Situation.UNREACHABLE, first_line(exc))
    try:
        try:
            listing = cast(DependentsIntrospection, adapter).dependents(inputs.schema, inputs.table)
        except Exception as exc:
            return finish(
                Situation.LISTING_FAILED,
                f"listing the objects that read {inputs.schema}.{inputs.table} failed: {first_line(exc)}",
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
        close_quietly(adapter)
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
