"""The ``probe_views`` validation op.

``probe_views`` tries each view of ``PROBE_VIEWS`` with a zero-row read and
reports which ones no longer bind, and never fails the op for a view that does
not. The block and the exit codes follow
:mod:`~continuo_python_runtime.validation.warehouse_read`: a failed probe or an
unreachable warehouse is exit 0, an input or adapter problem or an unexpected
error is exit 2.
"""
import logging
from collections.abc import Mapping
from typing import Any

from continuo_engine_contract.port import discover_adapter  # type: ignore[import-untyped]

from continuo_python_runtime.validation.warehouse_read import (
    Discover,
    InputError,
    close_quietly,
    discover_and_check_env,
    emit,
    first_line,
    json_list,
)

logger = logging.getLogger("validation_probe_views")


def _probe_views_input(env: Mapping[str, str]) -> list[tuple[str, str]]:
    """``PROBE_VIEWS`` as ``(schema, name)`` pairs, split on the first dot, each once, in order."""
    pairs: list[tuple[str, str]] = []
    for entry in json_list(env, "PROBE_VIEWS", required=True):
        schema, dot, name = entry.partition(".")
        if not dot or not schema or not name:
            raise InputError(f"PROBE_VIEWS entry {entry!r} is not <schema>.<name>")
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
    emit(status, message, node_id, reason, {"op": "probe_views", "checked": checked, "views": views})
    return 2 if status == "error" else 0


def run_probe_views(env: Mapping[str, str], discover: Discover = discover_adapter) -> int:
    """Run ``probe_views`` against *env*; print its one block and return the exit code.

    Reads ``PROBE_VIEWS`` and the adapter's ``required_env()`` from *env*. A view
    that fails its probe, or a warehouse that cannot be reached, still ends with
    exit 0; only an input or adapter problem or an unexpected error is exit 2.
    """
    try:
        return _probe_views(env, discover)
    except Exception as exc:
        logger.exception("probe_views failed unexpectedly")
        return _finish_probe(env, "error", f"unexpected error: {first_line(exc)}", "", False, [])


def _probe_views(env: Mapping[str, str], discover: Discover) -> int:
    try:
        pairs = _probe_views_input(env)
        _, adapter_cls = discover_and_check_env(env, discover)
    except InputError as exc:
        return _finish_probe(env, "error", str(exc), "", False, [])
    try:
        adapter = adapter_cls.from_env()
    except Exception as exc:
        return _finish_probe(
            env, "success", f"warehouse_unreachable: {first_line(exc)}", "warehouse_unreachable", False, []
        )
    views: list[dict[str, Any]] = []
    try:
        for schema, name in pairs:
            error = ""
            try:
                adapter.fetch(f"SELECT * FROM {_quote(schema)}.{_quote(name)} LIMIT 0")
            except Exception as exc:
                error = first_line(exc)
            views.append({"schema": schema, "name": name, "ok": not error, "error": error})
    finally:
        close_quietly(adapter)
    broken = [f"{v['schema']}.{v['name']}" for v in views if not v["ok"]]
    for name in broken:
        logger.warning("probe_views: %s does not bind", name)
    message = f"{len(broken)} of {len(views)} views failed: {', '.join(broken)}" if broken else ""
    return _finish_probe(env, "success", message, "", True, views)
