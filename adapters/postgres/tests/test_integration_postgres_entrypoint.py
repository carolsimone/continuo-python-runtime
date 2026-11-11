"""The validation ops and the harness through the image's entry point, against postgres:16.

Each test starts a fresh process the way the container does (``continuo-runtime
validation-op`` and ``continuo-runtime run``) with an environment built from
scratch: PATH, HOME and the stack's settings, nothing of the developer's shell.
The development environment installs every engine's adapter, while an image
installs one, so the process restricts adapter discovery to postgres before it
hands over to ``continuo_python_runtime.cli.main``; everything after that is the
real entry point.

The stdout contract under test: the process writes exactly one sentinel-framed
JSON result block and nothing else to stdout; diagnostics go to stderr.

Run the stack first:
    docker compose -f tests/smoke/postgres-stack/docker-compose.yml up -d --wait
"""
import contextlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg2
import pytest
import yaml

from continuo_engine_contract import result
from continuo_python_runtime.contract.merge import build_wire_contract

from .test_integration_postgres_conformance import PG

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

_ENGINE_ENV = {
    "POSTGRES_HOST": "localhost", "POSTGRES_PORT": str(PG["port"]), "POSTGRES_DB": PG["dbname"],
    "POSTGRES_USER": PG["user"], "POSTGRES_PASSWORD": PG["password"],
}


def _entrypoint(command: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run ``continuo-runtime <command>`` with *env* plus PATH and HOME only."""
    clean = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], **_ENGINE_ENV, **env}
    return subprocess.run(
        [sys.executable, "-c", _SHIM, "postgres", command],
        env=clean, capture_output=True, text=True, timeout=120,
    )


def _block(proc: subprocess.CompletedProcess) -> dict:
    """The one result block on stdout; stdout holds nothing else."""
    lines = proc.stdout.splitlines()
    assert len(lines) == 3, f"stdout is not exactly one block: {proc.stdout!r}\nstderr: {proc.stderr}"
    assert lines[0] == result.SENTINEL_BEGIN and lines[2] == result.SENTINEL_END
    return json.loads(lines[1])


def _connect():
    conn = psycopg2.connect(**PG)
    conn.autocommit = True
    return conn


def _query(sql: str, *params) -> list[tuple]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())
    finally:
        conn.close()


@pytest.fixture
def estate() -> Iterator[tuple[str, str]]:
    """A seed with a continuo view in schema ``s``, and a view of another owner in schema ``b``."""
    suffix = uuid.uuid4().hex[:10]
    s, b = f"ep_{suffix}", f"epb_{suffix}"
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE ROLE "{b}" NOLOGIN')
            cur.execute(f'CREATE SCHEMA "{s}"')
            cur.execute(f'CREATE SCHEMA "{b}" AUTHORIZATION "{b}"')
            cur.execute(f'CREATE TABLE "{s}".seed (id int)')
            cur.execute(f'CREATE VIEW "{s}".v AS SELECT id FROM "{s}".seed')
            cur.execute(f'CREATE VIEW "{b}".bi_v AS SELECT id FROM "{s}".seed')
            cur.execute(f'ALTER VIEW "{b}".bi_v OWNER TO "{b}"')
        yield s, b
    finally:
        with conn.cursor() as cur:
            for schema in (s, b):
                with contextlib.suppress(psycopg2.Error):
                    cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            with contextlib.suppress(psycopg2.Error):
                cur.execute(f'DROP OWNED BY "{b}"')
            with contextlib.suppress(psycopg2.Error):
                cur.execute(f'DROP ROLE IF EXISTS "{b}"')
        conn.close()


def _seed_env(s: str, **extra: str) -> dict[str, str]:
    return {
        "VALIDATION_OP": "seed_preflight", "SEED_PREFLIGHT_MODE": "check",
        "SEED_PREFLIGHT_ENGINE_FAMILY": "schema_bound", "PROD_SCHEMA": s, "TABLE_NAME": "seed",
        "SEED_PREFLIGHT_ALLOWED": json.dumps([f"{s}.v"]), **extra,
    }


def test_seed_preflight_stops_on_a_view_outside_the_allowed_set(estate):
    s, b = estate
    proc = _entrypoint("validation-op", _seed_env(s))
    block = _block(proc)
    assert proc.returncode == 1
    assert (block["status"], block["reason"], block["op"]) == ("error", "unowned_dependents", "seed_preflight")
    assert block["failures"] == 1 and block["checked"] is True and block["completeness"] == "exact"
    assert {(d["schema"], d["name"], d["allowed"]) for d in block["dependents"]} == {(s, "v", True), (b, "bi_v", False)}
    assert "unowned_dependents" in proc.stderr


def test_seed_preflight_passes_once_the_view_is_acknowledged(estate):
    s, b = estate
    proc = _entrypoint("validation-op", _seed_env(s, SEED_PREFLIGHT_ACKNOWLEDGED=json.dumps([f"{b}.bi_v"])))
    block = _block(proc)
    assert proc.returncode == 0
    assert (block["status"], block["op"], block["failures"]) == ("success", "seed_preflight", 0)
    assert "reason" not in block and all(d["allowed"] for d in block["dependents"])


def test_probe_views_reports_a_view_that_cannot_be_read_and_exits_0(estate):
    s, b = estate
    proc = _entrypoint("validation-op", {"VALIDATION_OP": "probe_views", "PROBE_VIEWS": json.dumps([f"{s}.v", f"{b}.bi_v"])})
    block = _block(proc)
    assert proc.returncode == 0
    assert (block["status"], block["op"], block["checked"]) == ("success", "probe_views", True)
    # The continuo role does not own bi_v, so reading it fails.
    assert [(v["name"], v["ok"]) for v in block["views"]] == [("v", True), ("bi_v", False)]
    assert "permission denied" in block["views"][1]["error"]
    assert "bi_v" in proc.stderr


@pytest.mark.parametrize("env,op", [
    ({"VALIDATION_OP": "seed_preflight", "SEED_PREFLIGHT_MODE": "drop"}, "seed_preflight"),
    ({"VALIDATION_OP": "probe_views", "PROBE_VIEWS": '"bi.x"'}, "probe_views"),
])
def test_a_bad_input_exits_2_and_the_process_does_not_ask_for_a_candidate_schema(env, op):
    proc = _entrypoint("validation-op", env)
    block = _block(proc)
    assert proc.returncode == 2
    assert (block["status"], block["op"], block["failures"]) == ("error", op, 1)
    assert "reason" not in block and block["message"]
    # The test's env has no DBT_TARGET_SCHEMA; a process that asked for it would
    # fail with a block naming the variable instead of reporting the bad input.
    assert "DBT_TARGET_SCHEMA" not in block["message"]
    assert block["message"] in proc.stderr


def test_the_harness_refuses_an_image_that_differs_from_the_release_and_writes_nothing(tmp_path: Path):
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
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{target}"')
            cur.execute(f'CREATE TABLE "{target}".existing (id int)')
        proc = _entrypoint("run", env)
        block = _block(proc)
        assert proc.returncode == 1
        assert (block["status"], block["reason"], block["failures"]) == ("error", "image_contract_mismatch", 1)
        assert block["message"].startswith("ImageContractMismatch: ")
        assert "ImageContractMismatch" in proc.stderr
        # The query sees a table that exists, so its answer that `t` is absent counts.
        assert _query("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", target) == [("existing",)]
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{target}" CASCADE')
        conn.close()
