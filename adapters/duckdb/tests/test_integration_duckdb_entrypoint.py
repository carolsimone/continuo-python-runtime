"""The validation ops and the harness through the image's entry point, against a real DuckLake.

Each test starts a fresh process the way the container does (``continuo-runtime
validation-op`` and ``continuo-runtime run``) with an environment built from
scratch: PATH, HOME and the stack's settings (``lake_env`` from this directory's
conftest.py), nothing of the developer's shell. The development environment
installs every engine's adapter, while an image installs one, so the process
restricts adapter discovery to duckdb before it hands over to
``continuo_python_runtime.cli.main``; everything after that is the real entry
point.

The stdout contract under test: the process writes exactly one sentinel-framed
JSON result block and nothing else to stdout; diagnostics go to stderr.

Run the stack first:
    docker compose -f tests/smoke/duckdb-stack/docker-compose.yml up -d --wait
"""
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from continuo_engine_contract import result
from continuo_duckdb_adapter.adapter import DuckDBAdapter
from continuo_python_runtime.contract.merge import build_wire_contract

pytestmark = pytest.mark.integration

# The same text is in the entry-point tests of postgres, trino and duckdb
# (tests/test_entrypoint_shims.py checks the copies agree): each adapter test
# directory is its own pytest package, so there is no helper to share. The
# development venv installs all three adapters while an image installs one, and
# discovery refuses several, so the process keeps only the engine named in
# argv[1] before it runs the real CLI.
_SHIM = (
    "import sys\n"
    "import continuo_engine_contract.port as port\n"
    "real = port.entry_points\n"
    "port.entry_points = lambda **kw: [e for e in real(**kw) if e.name == sys.argv[1]]\n"
    "from continuo_python_runtime import cli\n"
    "sys.exit(cli.main(sys.argv[2:]))\n"
)


CREATE_EXISTING = 'CREATE TABLE "{schema}"."existing" (id integer)'


def _entrypoint(command: str, lake_env: dict[str, str], env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run ``continuo-runtime <command>`` with *env* plus PATH, HOME and the lake's settings only."""
    clean = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], **lake_env, **env}
    return subprocess.run(
        [sys.executable, "-c", _SHIM, "duckdb", command],
        env=clean, capture_output=True, text=True, timeout=180,
    )


def _block(proc: subprocess.CompletedProcess) -> dict:
    """The one result block on stdout; stdout holds nothing else."""
    lines = proc.stdout.splitlines()
    assert len(lines) == 3, f"stdout is not exactly one block: {proc.stdout!r}\nstderr: {proc.stderr}"
    assert lines[0] == result.SENTINEL_BEGIN and lines[2] == result.SENTINEL_END
    return json.loads(lines[1])


def _tables(adapter: DuckDBAdapter, schema: str) -> list[str]:
    rows = adapter.fetch(
        f"SELECT table_name FROM information_schema.tables WHERE table_schema = '{schema}' ORDER BY 1"
    )
    return [str(name) for name in rows.column(0).to_pylist()]


@pytest.fixture
def lake(adapter_factory) -> Iterator[tuple[DuckDBAdapter, str]]:
    """A connected adapter and a fresh schema in the lake; the schema is dropped at the end."""
    adapter = adapter_factory()
    schema = f"ep_{uuid.uuid4().hex[:10]}"
    try:
        adapter.ensure_schema(schema)
        yield adapter, schema
    finally:
        adapter.drop_schema(schema)


def _seed_env(s: str, family: str, **extra: str) -> dict[str, str]:
    return {
        "VALIDATION_OP": "seed_preflight", "SEED_PREFLIGHT_MODE": "check",
        "SEED_PREFLIGHT_ENGINE_FAMILY": family, "PROD_SCHEMA": s, "TABLE_NAME": "seed", **extra,
    }


def test_seed_preflight_on_a_late_binding_release_passes_without_listing(lake, lake_env):
    _, s = lake
    proc = _entrypoint("validation-op", lake_env, _seed_env(s, "late_binding"))
    block = _block(proc)
    assert proc.returncode == 0
    assert (block["status"], block["op"], block["failures"]) == ("success", "seed_preflight", 0)
    # DuckDB cannot list the objects that read a table, so nothing is listed.
    assert "reason" not in block and block["checked"] is False and block["completeness"] == "none"
    assert block["dependents"] == [] and block["drop_semantics"] == "late_binding"


def test_seed_preflight_stops_a_schema_bound_release_on_duckdb(lake, lake_env):
    _, s = lake
    proc = _entrypoint("validation-op", lake_env, _seed_env(s, "schema_bound"))
    block = _block(proc)
    assert proc.returncode == 1
    assert (block["status"], block["reason"], block["op"]) == ("error", "engine_mismatch", "seed_preflight")
    assert block["failures"] == 1 and block["checked"] is False and block["completeness"] == "none"
    assert "engine_mismatch" in proc.stderr


def test_probe_views_reports_a_view_that_no_longer_binds_and_exits_0(lake, lake_env):
    adapter, s = lake
    for statement in (
        f'CREATE TABLE "{s}"."seed" (id integer)',
        f'CREATE VIEW "{s}"."ok" AS SELECT id FROM "{s}"."seed"',
        f'CREATE TABLE "{s}"."t2" (id integer)',
        f'CREATE VIEW "{s}"."broken" AS SELECT id FROM "{s}"."t2"',
        f'DROP TABLE "{s}"."t2"',
    ):
        adapter.fetch(statement)
    proc = _entrypoint(
        "validation-op", lake_env, {"VALIDATION_OP": "probe_views", "PROBE_VIEWS": json.dumps([f"{s}.ok", f"{s}.broken"])}
    )
    block = _block(proc)
    assert proc.returncode == 0
    assert (block["status"], block["op"], block["checked"]) == ("success", "probe_views", True)
    assert [(v["name"], v["ok"]) for v in block["views"]] == [("ok", True), ("broken", False)]
    assert "does not exist" in block["views"][1]["error"] and f"{s}.broken" in proc.stderr


@pytest.mark.parametrize("env,op", [
    ({"VALIDATION_OP": "seed_preflight", "SEED_PREFLIGHT_MODE": "drop"}, "seed_preflight"),
    ({"VALIDATION_OP": "probe_views", "PROBE_VIEWS": '"bi.x"'}, "probe_views"),
])
def test_a_bad_input_exits_2_and_the_process_does_not_ask_for_a_candidate_schema(lake_env, env, op):
    proc = _entrypoint("validation-op", lake_env, env)
    block = _block(proc)
    assert proc.returncode == 2
    assert (block["status"], block["op"], block["failures"]) == ("error", op, 1)
    assert "reason" not in block and block["message"]
    # The test's env has no DBT_TARGET_SCHEMA; a process that asked for it would
    # fail with a block naming the variable instead of reporting the bad input.
    assert "DBT_TARGET_SCHEMA" not in block["message"]
    assert block["message"] in proc.stderr


def test_the_harness_refuses_an_image_that_differs_from_the_release_and_writes_nothing(tmp_path: Path, lake, lake_env):
    adapter, _ = lake
    (tmp_path / "contracts").mkdir()
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "t.py"
    script.write_text("def run(ctx):\n    return ctx.read('ids')\n")
    (tmp_path / "contracts" / "t.yml").write_text(yaml.safe_dump({"nodes": [{
        "schema": "analytics", "table": "t", "owner": "m", "schedule": "daily",
        "criticality": "SECONDARY", "script": "scripts/t.py",
        "reads": {"ids": "select id from analytics.a"},
        "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
    }]}))
    released = build_wire_contract(tmp_path / "contracts", tmp_path, "s")["nodes"][0]["content_hash"]
    script.write_text("def run(ctx):\n    return ctx.read('ids')  # edited after the release\n")
    target = f"ep_{uuid.uuid4().hex[:10]}"
    env = {
        "NODE_ID": "python-node.svc.analytics.t", "TABLE_NAME": "t", "TARGET_SCHEMA": target,
        "CONTRACT_DIR": str(tmp_path / "contracts"), "APP_ROOT": str(tmp_path), "CONTENT_HASH": released,
    }
    try:
        adapter.ensure_schema(target)
        adapter.fetch(CREATE_EXISTING.format(schema=target))
        proc = _entrypoint("run", lake_env, env)
        block = _block(proc)
        assert proc.returncode == 1
        assert (block["status"], block["reason"], block["failures"]) == ("error", "image_contract_mismatch", 1)
        assert block["message"].startswith("ImageContractMismatch: ")
        assert "ImageContractMismatch" in proc.stderr
        # The query sees a table that exists, so its answer that `t` is absent counts.
        assert _tables(adapter, target) == ["existing"]
    finally:
        adapter.drop_schema(target)
