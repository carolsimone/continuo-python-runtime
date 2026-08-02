"""Container entrypoint: dispatches a single node's script and writes its output.

``run_node`` is the sole write sink for a python-model node. It resolves the
node from the contract, loads and executes its script inside a
:class:`~continuo_python_runtime.context.RunContext`, conforms the result to
the declared schema, and writes it through the runtime adapter. Exactly one
sentinel-framed result block is printed to stdout per run; every other
diagnostic goes to stderr via ``logging``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import os
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

from continuo_validation_contract.port import (  # type: ignore[import-untyped]
    discover_runtime_adapter,
)
from continuo_validation_contract.result import result_block  # type: ignore[import-untyped]

from continuo_python_runtime.conform import conform, to_arrow
from continuo_python_runtime.context import RunContext
from continuo_python_runtime.contract.loader import load_contract_dir
from continuo_python_runtime.contract.model import Node
from continuo_python_runtime.errors import ContractError, HarnessError, LoadError, ScriptError

logger = logging.getLogger("continuo_python_runtime.harness")


def _require_env(env: Mapping[str, str], key: str) -> str:
    """Return env[key], raising ContractError if it is missing or empty."""
    value = env.get(key)
    if not value:
        raise ContractError(f"missing required environment variable {key!r}")
    return value


def select_node(nodes: list[Node], node_id: str) -> Node:
    """Select the node matching the trailing ``schema.table`` of ``node_id``.

    ``node_id`` is split on ``.``; the trailing two segments are taken as
    ``(schema, table)`` and matched against the declared nodes.

    Raises:
        ContractError: If ``node_id`` has fewer than 2 dot-separated
            segments, or no declared node matches.
    """
    segments = node_id.split(".")
    if len(segments) < 2:
        raise ContractError(
            f"node_id {node_id!r} must have at least 2 dot-separated segments (schema.table)"
        )
    schema, table = segments[-2], segments[-1]
    for node in nodes:
        if node.schema == schema and node.table == table:
            return node

    available = sorted(f"{n.schema}.{n.table}" for n in nodes)
    raise ContractError(
        f"no node matches {node_id!r} (schema={schema!r}, table={table!r}); "
        f"available relations: {available}"
    )


def load_script(node: Node, repo_root: Path) -> ModuleType:
    """Import ``node.script`` (relative to ``repo_root``) and return the module.

    Raises:
        ContractError: If the script path is absolute, escapes the
            repository root, or does not exist.
        ScriptError: If the module has no callable ``run``.
    """
    if Path(node.script).is_absolute():
        raise ContractError(
            f"script path {node.script!r} must be relative to the repository root"
        )

    script_path = (repo_root / node.script).resolve()
    if not script_path.is_relative_to(repo_root.resolve()):
        raise ContractError(f"script path {node.script!r} escapes the repository root")
    if not script_path.is_file():
        raise ContractError(f"script not found: {node.script}")

    module_name = f"_continuo_node_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise ContractError(f"cannot load script: {node.script}")

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ScriptError(f"script import failed: {exc}") from exc

    if not callable(getattr(module, "run", None)):
        raise ScriptError(f"script {node.script} has no callable 'run'")

    return module


def build_adapter() -> Any:
    """Discover and construct the single installed runtime adapter.

    Raises:
        LoadError: If any of the adapter's ``required_env()`` vars are unset
            or empty in ``os.environ``.
    """
    _, cls = discover_runtime_adapter()
    required = cls.required_env()
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise LoadError(f"missing required warehouse env: {sorted(missing)}")
    return cls.from_env()


def _execute_script(module: ModuleType, ctx: RunContext) -> Any:
    """Run ``module.run(ctx)`` with stdout redirected to stderr.

    Raises:
        HarnessError: Propagated as-is (e.g. a ``ReadError`` from ``ctx.read``).
        ScriptError: Wraps any other exception raised by user code.
    """
    with contextlib.redirect_stdout(sys.stderr):
        try:
            return module.run(ctx)
        except HarnessError:
            raise
        except Exception as exc:
            raise ScriptError(f"run() raised {exc.__class__.__name__}: {exc}") from exc


def run_node(env: Mapping[str, str], adapter: Any = None) -> int:
    """Run a single node end-to-end and print exactly one sentinel result block.

    Returns 0 on success, 1 on any :class:`HarnessError`.
    """
    node_id = env.get("NODE_ID") or ""
    active_adapter: Any = None
    try:
        node_id = _require_env(env, "NODE_ID")
        table_name = _require_env(env, "TABLE_NAME")
        target_schema = _require_env(env, "TARGET_SCHEMA")

        contract_dir = Path(env.get("CONTRACT_DIR") or "/app/contracts")
        app_root = Path(env["APP_ROOT"]) if env.get("APP_ROOT") else contract_dir.parent

        logger.info("running node %s -> %s.%s", node_id, target_schema, table_name)

        nodes = load_contract_dir(contract_dir)
        node = select_node(nodes, node_id)

        with contextlib.redirect_stdout(sys.stderr):
            module = load_script(node, app_root)

        if adapter is not None:
            active_adapter = adapter
        else:
            try:
                active_adapter = build_adapter()
            except HarnessError:
                raise
            except Exception as exc:
                raise LoadError(f"adapter construction failed: {exc}") from exc

        ctx = RunContext(node, active_adapter)
        raw_result = _execute_script(module, ctx)

        table = to_arrow(raw_result)
        conformed = conform(table, node.output_columns, node.extra_columns)

        columns = [
            {"name": c.name, "type": c.type, "nullable": c.nullable}
            for c in node.output_columns
        ]
        try:
            active_adapter.ensure_table(target_schema, table_name, columns)
            active_adapter.load(target_schema, table_name, conformed)
        except HarnessError:
            raise
        except Exception as exc:
            raise LoadError(f"failed to write {target_schema}.{table_name}: {exc}") from exc

        print(result_block("success", message=f"rows={conformed.num_rows}", unique_id=node_id))
        return 0
    except HarnessError as err:
        print(
            result_block(
                "error", message=err.sentinel_message(), failures=1, unique_id=node_id
            )
        )
        return 1
    except Exception as exc:
        logger.exception("unexpected failure running node %s", node_id)
        print(
            result_block(
                "error",
                message=f"ScriptError: unexpected failure: {exc}",
                failures=1,
                unique_id=node_id,
            )
        )
        return 1
    finally:
        if active_adapter is not None:
            try:
                active_adapter.close()
            except Exception:
                logger.warning("adapter.close() failed", exc_info=True)
