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
from continuo_python_runtime.contract.paths import resolve_script_path
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


def ensure_import_paths(repo_root: Path, script_dir: Path) -> None:
    """Prepend ``repo_root`` then ``script_dir`` to ``sys.path``, idempotently.

    ``importlib``'s ``spec_from_file_location`` + ``exec_module`` executes a
    file without putting anything on ``sys.path``, and ``continuo-runtime`` is
    an installed console script, so ``sys.path[0]`` is the venv's ``bin``
    directory — ``/app`` is on no path. A node script importing its own
    shared helpers (exactly what ``shared_code_hash`` folds into the content
    hash) would therefore die with ``ModuleNotFoundError`` on its first
    production run, having passed CI and the domain repo's own pytest, which
    adds the rootdir to ``sys.path`` itself.

    The two roots and their order mirror
    :func:`continuo_python_runtime.closure._resolve_name`'s search roots, so a
    name the closure resolver folded into the hash is a name the interpreter
    can resolve: ``repo_root`` first (package-qualified ``import
    scripts.helpers``, or a helper in a sibling ``lib/``), then the importing
    script's own directory (sibling ``import helpers``).

    Entries are left in place for the process lifetime rather than removed
    after ``exec_module``: a script may import lazily inside ``run()``, and a
    container runs exactly one node per process. Both paths are resolved
    first, and each is unconditionally repositioned to the front rather than
    left wherever it already was: the shipped images set ``PYTHONPATH=/app``,
    so ``repo_root`` is *always* already on ``sys.path`` before this runs, and
    merely skipping an already-present entry would leave it behind
    ``script_dir`` -- the reverse of the required order. An existing entry is
    removed before being reinserted at index 0, so repeated calls still
    cannot grow ``sys.path`` or duplicate an entry, however the caller spelled
    the roots or whatever order they were already in.
    """
    for entry in (str(script_dir.resolve()), str(repo_root.resolve())):
        if entry in sys.path:
            sys.path.remove(entry)
        sys.path.insert(0, entry)


def load_script(node: Node, repo_root: Path) -> ModuleType:
    """Import ``node.script`` (relative to ``repo_root``) and return the module.

    ``repo_root`` and the script's own directory are put on ``sys.path`` first
    (see :func:`ensure_import_paths`) so the script's in-repo import closure
    is importable.

    Raises:
        ContractError: If the script path is absolute, escapes the
            repository root, or does not exist.
        ScriptError: If the module has no callable ``run``.
    """
    script_path = resolve_script_path(node.script, repo_root, context=node.relation)
    ensure_import_paths(repo_root, script_path.parent)

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


def _validate_config_early(adapter: Any, node: Node) -> None:
    """Check ``node.config`` against the engine's vocabulary before anything runs.

    ``ensure_table`` validates ``config`` too and stays the enforcement point —
    but it is called *after* the script has executed and its result has been
    conformed, so a single typo (``config: {index: [...]}``) burned the whole
    node run before failing. This is the tripwire that fails it in the first
    second instead. Validation-time checking of the same vocabulary is a future
    cross-repo dependency (continuo-validation step 3a), and `continuo-runtime
    validate` runs on a CI runner where no engine adapter is installed at all,
    so this is the earliest point the engine's own rules can be applied.

    The call is skipped for an adapter that does not provide ``validate_config``:
    the abstract ``RuntimeAdapter`` in the pinned ``continuo-validation-contract``
    does not declare it (this repo ships the harness and both adapters as one
    coordinated release), and an adapter without it loses only earliness —
    ``ensure_table`` still fails closed on the same config.

    Raises:
        LoadError: If the adapter rejects the config, matching how a rejection
            from ``ensure_table`` surfaces.
    """
    validate = getattr(adapter, "validate_config", None)
    if validate is None:
        return
    column_names = [c.name for c in node.output_columns]
    try:
        validate(node.config, column_names)
    except HarnessError:
        raise
    except Exception as exc:
        raise LoadError(
            f"invalid config for {node.relation}: {exc}"
        ) from exc


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

        # check_reads=False: the harness has no --dialect of its own, so
        # re-running the read-shape gate here would judge the reads against
        # sqlglot's dialect-neutral grammar -- a different, in places stricter
        # grammar than the one CI used -- and could only disagree with the
        # gates already passed (CI's `continuo-runtime validate`, then
        # Continuo's own parser and bind-check), never catch a new problem.
        # ctx.read resolves declared reads by name and never parses them.
        # Every other loader validation still runs at container start.
        nodes = load_contract_dir(contract_dir, check_reads=False)
        node = select_node(nodes, node_id)

        if adapter is not None:
            active_adapter = adapter
        else:
            try:
                active_adapter = build_adapter()
            except HarnessError:
                raise
            except Exception as exc:
                raise LoadError(f"adapter construction failed: {exc}") from exc

        _validate_config_early(active_adapter, node)

        with contextlib.redirect_stdout(sys.stderr):
            module = load_script(node, app_root)

        ctx = RunContext(node, active_adapter)
        raw_result = _execute_script(module, ctx)

        table = to_arrow(raw_result)
        conformed = conform(table, node.output_columns, node.extra_columns)

        columns = [
            {"name": c.name, "type": c.type, "nullable": c.nullable}
            for c in node.output_columns
        ]
        try:
            active_adapter.ensure_table(target_schema, table_name, columns, config=node.config)
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
