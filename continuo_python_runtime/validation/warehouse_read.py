"""What the warehouse-read validation ops share: their inputs, their one block and their adapter.

The ops ``dependents_check`` (:mod:`~continuo_python_runtime.validation.dependents_check`)
and ``probe_views`` (:mod:`~continuo_python_runtime.validation.probe_views`) read the
warehouse and change nothing. Each prints exactly one structured result block as
the last line on stdout (``continuo_engine_contract.result``) and returns the
process exit code: 0 when the op ran to its conclusion, 1 when a ``check`` stops,
2 when the op could not run (a bad input, no adapter, missing adapter env, an
unexpected error). The block's ``status`` is ``error`` for exit codes 1 and 2.
Diagnostics go to stderr through ``logging``; stdout carries only the block.
"""
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any, cast

from continuo_engine_contract import result  # type: ignore[import-untyped]
from continuo_engine_contract.port import (  # type: ignore[import-untyped]
    AdapterDiscoveryError,
    WarehouseAdapter,
)

logger = logging.getLogger("validation_warehouse_read")

Discover = Callable[[], tuple[str, type[WarehouseAdapter]]]

# Longest driver text kept in a block; the first line only, so a block stays small.
MAX_TEXT = 500


class InputError(Exception):
    """An input of the op is missing or malformed."""


def first_line(exc: BaseException) -> str:
    """The first line of an exception's text, at most ``MAX_TEXT`` characters, never empty."""
    text = str(exc).strip()
    line = text.splitlines()[0] if text else ""
    return (line or type(exc).__name__)[:MAX_TEXT]


def json_list(env: Mapping[str, str], name: str, *, required: bool) -> list[str]:
    """The env var *name* as a JSON array of strings; unset or empty is ``[]`` unless *required*."""
    raw = env.get(name, "")
    if not raw.strip():
        if required:
            raise InputError(f"missing required env var {name}")
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError(f"{name} is not valid JSON: {exc}") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InputError(f"{name} must be a JSON array of strings")
    return cast(list[str], value)


def emit(
    status: str, message: str, unique_id: str, reason: str, fields: dict[str, Any]
) -> None:
    """Print the op's one result block, the last line on stdout."""
    print(
        result.result_block(
            status, message, 1 if status == "error" else 0, unique_id, reason=reason, fields=fields
        ),
        flush=True,
    )


def discover_and_check_env(
    env: Mapping[str, str], discover: Discover
) -> tuple[str, type[WarehouseAdapter]]:
    """The installed adapter, after checking its required env is present in *env*."""
    try:
        engine, adapter_cls = discover()
    except AdapterDiscoveryError as exc:
        raise InputError(str(exc)) from exc
    missing = [name for name in adapter_cls.required_env() if not env.get(name)]
    if missing:
        raise InputError(f"missing required env for engine {engine!r}: {', '.join(missing)}")
    return engine, adapter_cls


def close_quietly(adapter: Any) -> None:
    """Close the adapter; a failure only logs, so it never masks the op's outcome."""
    try:
        adapter.close()
    except Exception as exc:
        logger.error("adapter close failed: %s", exc)
