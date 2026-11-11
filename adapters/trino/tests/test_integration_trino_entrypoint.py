"""The validation ops and the harness through the image's entry point, against Trino + Iceberg.

Each test starts a fresh process the way the container does (``continuo-runtime
validation-op`` and ``continuo-runtime run``) with an environment built from
scratch: PATH, HOME and the stack's settings, nothing of the developer's shell.
The development environment installs every engine's adapter, while an image
installs one, so the process restricts adapter discovery to trino before it
hands over to ``continuo_python_runtime.cli.main``; everything after that is the
real entry point.

The stdout contract under test: the process writes exactly one sentinel-framed
JSON result block and nothing else to stdout; diagnostics go to stderr.

Run the stack first:
    docker compose -f tests/smoke/trino-stack/docker-compose.yml up -d --wait
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
from continuo_python_runtime.contract.merge import build_wire_contract
from continuo_trino_adapter.adapter import TrinoAdapter

pytestmark = pytest.mark.integration

_SHIM = (
    "import sys\n"
    "import continuo_engine_contract.port as port\n"
    "real = port.entry_points\n"
    "port.entry_points = lambda **kw: [e for e in real(**kw) if e.name == sys.argv[1]]\n"
    "from continuo_python_runtime import cli\n"
    "sys.exit(cli.main(sys.argv[2:]))\n"
)


_ENGINE_ENV = {
    "TRINO_HOST": "localhost", "TRINO_PORT": os.environ.get("VR_IT_TRINO_PORT", "18080"),
    "TRINO_USER": "continuo", "TRINO_CATALOG": "iceberg", "TRINO_HTTP_SCHEME": "http",
}
CREATE_EXISTING = 'CREATE TABLE "iceberg"."{schema}"."existing" (id integer)'


def _entrypoint(command: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run ``continuo-runtime <command>`` with *env* plus PATH and HOME only."""
    clean = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], **_ENGINE_ENV, **env}
    return subprocess.run(
        [sys.executable, "-c", _SHIM, "trino", command],
        env=clean, capture_output=True, text=True, timeout=180,
    )


def _block(proc: subprocess.CompletedProcess) -> dict:
    """The one result block on stdout; stdout holds nothing else."""
    lines = proc.stdout.splitlines()
    assert len(lines) == 3, f"stdout is not exactly one block: {proc.stdout!r}\nstderr: {proc.stderr}"
    assert lines[0] == result.SENTINEL_BEGIN and lines[2] == result.SENTINEL_END
    return json.loads(lines[1])


def _tables(adapter: TrinoAdapter, schema: str) -> list[str]:
    rows = adapter.fetch(
        f"SELECT table_name FROM iceberg.information_schema.tables WHERE table_schema = '{schema}' ORDER BY 1"
    )
    return [str(name) for name in rows.column(0).to_pylist()]


@pytest.fixture
def lake(monkeypatch) -> Iterator[tuple[TrinoAdapter, str]]:
    """A connected adapter and a fresh schema; the schema is dropped at the end."""
    for key, value in _ENGINE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TRINO_PASSWORD", raising=False)
    adapter = TrinoAdapter.from_env()
    schema = f"ep_{uuid.uuid4().hex[:10]}"
    try:
        adapter.ensure_schema(schema)
        yield adapter, schema
    finally:
        try:
            adapter.drop_schema(schema)
        finally:
            adapter.close()


def _view(adapter: TrinoAdapter, schema: str, name: str, source_schema: str, source: str) -> None:
    adapter._execute(
        f'CREATE VIEW "iceberg"."{schema}"."{name}" AS SELECT "id" FROM "iceberg"."{source_schema}"."{source}"'
    )


def _seed_env(s: str, family: str, **extra: str) -> dict[str, str]:
    return {
        "VALIDATION_OP": "seed_preflight", "SEED_PREFLIGHT_MODE": "check",
        "SEED_PREFLIGHT_ENGINE_FAMILY": family, "PROD_SCHEMA": s, "TABLE_NAME": "seed",
        "SEED_PREFLIGHT_ALLOWED": json.dumps([f"{s}.cv"]), **extra,
    }


def test_seed_preflight_on_a_late_binding_release_lists_an_external_view_and_passes(lake):
    adapter, s = lake
    other = f"{s}o"
    try:
        adapter.ensure_schema(other)
        adapter._execute(f'CREATE TABLE "iceberg"."{s}"."seed" (id integer)')
        _view(adapter, s, "cv", s, "seed")
        _view(adapter, other, "ext", s, "seed")
        proc = _entrypoint("validation-op", _seed_env(s, "late_binding"))
        block = _block(proc)
    finally:
        adapter.drop_schema(other)
    assert proc.returncode == 0
    assert (block["status"], block["op"], block["failures"]) == ("success", "seed_preflight", 0)
    assert "reason" not in block and block["checked"] is True and block["completeness"] == "best_effort"
    assert {(d["schema"], d["name"], d["allowed"]) for d in block["dependents"]} == {(s, "cv", True), (other, "ext", False)}
    assert "may go stale" in block["message"] and f"{other}.ext" in block["message"]


def test_seed_preflight_stops_a_schema_bound_release_on_trino(lake):
    _, s = lake
    proc = _entrypoint("validation-op", _seed_env(s, "schema_bound"))
    block = _block(proc)
    assert proc.returncode == 1
    assert (block["status"], block["reason"], block["op"]) == ("error", "engine_mismatch", "seed_preflight")
    assert block["failures"] == 1 and block["checked"] is False and block["completeness"] == "none"
    assert "engine_mismatch" in proc.stderr


def test_probe_views_reports_a_view_that_no_longer_binds_and_exits_0(lake):
    adapter, s = lake
    adapter._execute(f'CREATE TABLE "iceberg"."{s}"."seed" (id integer)')
    adapter._execute(f'CREATE TABLE "iceberg"."{s}"."t2" (id integer)')
    _view(adapter, s, "ok", s, "seed")
    _view(adapter, s, "broken", s, "t2")
    adapter._execute(f'DROP TABLE "iceberg"."{s}"."t2"')
    proc = _entrypoint("validation-op", {"VALIDATION_OP": "probe_views", "PROBE_VIEWS": json.dumps([f"{s}.ok", f"{s}.broken"])})
    block = _block(proc)
    assert proc.returncode == 0
    assert (block["status"], block["op"], block["checked"]) == ("success", "probe_views", True)
    assert [(v["name"], v["ok"]) for v in block["views"]] == [("ok", True), ("broken", False)]
    assert block["views"][1]["error"] and f"{s}.broken" in proc.stderr


@pytest.mark.parametrize("env,op", [
    ({"VALIDATION_OP": "seed_preflight", "SEED_PREFLIGHT_MODE": "drop"}, "seed_preflight"),
    ({"VALIDATION_OP": "probe_views", "PROBE_VIEWS": '"bi.x"'}, "probe_views"),
])
def test_a_bad_input_exits_2_without_a_candidate_schema(env, op):
    assert "DBT_TARGET_SCHEMA" not in env
    proc = _entrypoint("validation-op", env)
    block = _block(proc)
    assert proc.returncode == 2
    assert (block["status"], block["op"], block["failures"]) == ("error", op, 1)
    assert "reason" not in block and block["message"]
    assert block["message"] in proc.stderr


def test_the_harness_refuses_an_image_that_differs_from_the_release_and_writes_nothing(tmp_path: Path, lake):
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
        proc = _entrypoint("run", env)
        block = _block(proc)
        assert proc.returncode == 1
        assert (block["status"], block["reason"], block["failures"]) == ("error", "image_contract_mismatch", 1)
        assert block["message"].startswith("ImageContractMismatch: ")
        assert "ImageContractMismatch" in proc.stderr
        # The query sees a table that exists, so its answer that `t` is absent counts.
        assert _tables(adapter, target) == ["existing"]
    finally:
        adapter.drop_schema(target)
