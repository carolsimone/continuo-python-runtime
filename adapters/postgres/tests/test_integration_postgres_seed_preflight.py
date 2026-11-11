"""``seed_preflight`` against postgres:16 (tests/smoke/postgres-stack).

Run the stack first:
    docker compose -f tests/smoke/postgres-stack/docker-compose.yml up -d --wait

Every fixture creates uniquely named schemas and a NOLOGIN role and drops them in
teardown, whether the test passed or not.
"""
import contextlib
import io
import json
import os
import socket
import uuid
from collections.abc import Iterator

import psycopg2
import pytest

from continuo_engine_contract import result
from continuo_postgres_adapter import adapter as adapter_module
from continuo_postgres_adapter.adapter import PostgresAdapter
from continuo_python_runtime.validation import preflight

from .test_integration_postgres_conformance import PG

pytestmark = pytest.mark.integration


def _connect():
    conn = psycopg2.connect(**PG)
    conn.autocommit = True
    return conn


@pytest.fixture
def estate(monkeypatch) -> Iterator[tuple[str, str]]:
    """A seed with a continuo view in schema ``s``, and a view of another owner in schema ``b``."""
    suffix = uuid.uuid4().hex[:10]
    s, b = f"pf_{suffix}", f"pfb_{suffix}"
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
        monkeypatch.setenv("POSTGRES_HOST", "localhost")
        monkeypatch.setenv("POSTGRES_PORT", str(PG["port"]))
        monkeypatch.setenv("POSTGRES_DB", PG["dbname"])
        monkeypatch.setenv("POSTGRES_USER", PG["user"])
        monkeypatch.setenv("POSTGRES_PASSWORD", PG["password"])
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


def _run(op, **env):
    out = io.StringIO()
    run = preflight.run_seed_preflight if op == "seed_preflight" else preflight.run_probe_views
    with contextlib.redirect_stdout(out):
        code = run({**os.environ, **env}, discover=lambda: ("postgres", PostgresAdapter))
    lines = out.getvalue().splitlines()
    assert lines[-3] == result.SENTINEL_BEGIN and lines[-1] == result.SENTINEL_END
    return code, json.loads(lines[-2])


def _seed(s, mode="check", table="seed", allowed=(), acknowledged=(), **env):
    return _run(
        "seed_preflight", SEED_PREFLIGHT_MODE=mode, SEED_PREFLIGHT_ENGINE_FAMILY="schema_bound",
        PROD_SCHEMA=s, TABLE_NAME=table, SEED_PREFLIGHT_ALLOWED=json.dumps(list(allowed)),
        SEED_PREFLIGHT_ACKNOWLEDGED=json.dumps(list(acknowledged)), **env,
    )


def test_check_stops_on_a_view_continuo_does_not_own(estate):
    s, b = estate
    code, block = _seed(s, allowed=[f"{s}.v"])
    assert code == 1 and block["status"] == "error"
    assert block["reason"] == "unowned_dependents" and block["completeness"] == "exact"
    assert block["checked"] is True and block["drop_semantics"] == "cascade"
    assert {(d["schema"], d["name"], d["allowed"]) for d in block["dependents"]} == {(s, "v", True), (b, "bi_v", False)}
    assert f"{b}.bi_v (view, owner {b})" in block["message"]


def test_check_passes_once_the_operator_acknowledges_the_view(estate):
    s, b = estate
    code, block = _seed(s, allowed=[f"{s}.v"], acknowledged=[f"{b}.bi_v"])
    assert code == 0 and block["status"] == "success" and "reason" not in block
    assert all(d["allowed"] for d in block["dependents"])


def test_report_lists_the_same_objects_and_never_fails(estate):
    s, b = estate
    code, block = _seed(s, mode="report", allowed=[f"{s}.v"])
    assert code == 0 and block["status"] == "success" and block["reason"] == "unowned_dependents"
    assert {(d["schema"], d["name"]) for d in block["dependents"]} == {(s, "v"), (b, "bi_v")}


def test_check_of_a_seed_table_that_does_not_exist_yet_passes(estate):
    s, _ = estate
    code, block = _seed(s, table="new_seed")
    assert code == 0 and block["status"] == "success" and block["dependents"] == []
    assert block["checked"] is True


def test_probe_names_a_view_that_does_not_exist_or_cannot_be_read(estate):
    s, b = estate
    code, block = _run("probe_views", PROBE_VIEWS=json.dumps([f"{s}.v", f"{b}.bi_v", f"{s}.missing"]))
    assert code == 0 and block["status"] == "success" and block["checked"] is True
    assert [(v["name"], v["ok"]) for v in block["views"]] == [("v", True), ("bi_v", False), ("missing", False)]
    assert "permission denied" in block["views"][1]["error"]
    assert "does not exist" in block["views"][2]["error"]
    assert all("\n" not in v["error"] for v in block["views"])
    assert block["message"] == f"2 of 3 views failed: {b}.bi_v, {s}.missing"


def test_an_unreachable_warehouse_is_warehouse_unreachable(estate, monkeypatch):
    s, _ = estate
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_PORT", str(port))
    monkeypatch.setattr(adapter_module, "_CONNECT_RETRY_SECONDS", 0.01)
    code, block = _seed(s)
    assert code == 1 and block["status"] == "error"
    assert block["reason"] == "warehouse_unreachable" and block["checked"] is False
    code, block = _run("probe_views", PROBE_VIEWS=json.dumps([f"{s}.v"]))
    assert code == 0 and block["reason"] == "warehouse_unreachable" and block["views"] == []
