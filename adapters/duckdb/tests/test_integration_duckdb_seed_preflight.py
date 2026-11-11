"""``seed_preflight`` and ``probe_views`` against a real DuckLake (tests/smoke/duckdb-stack).

The ``lake_env`` and ``adapter_factory`` fixtures come from this directory's
conftest.py. DuckDB declares ``drop_semantics = "late_binding"`` and cannot list
the objects that read a table, so ``seed_preflight`` never connects to this
engine; the tests that expect that run once against the live lake and once with
the catalog behind a refusing port, where a connection attempt would show as
``warehouse_unreachable``.
Run the stack first:
    docker compose -f tests/smoke/duckdb-stack/docker-compose.yml up -d --wait
"""
import contextlib
import io
import json
import os
import socket
import uuid
from collections.abc import Iterator

import pytest

from continuo_engine_contract import result
from continuo_duckdb_adapter.adapter import DuckDBAdapter
from continuo_duckdb_adapter.infrastructure import session as session_module
from continuo_python_runtime.validation import preflight

pytestmark = pytest.mark.integration


def _refusing_port() -> int:
    """A local port nothing listens on. The socket is closed after reading the port:
    on macOS a bound but unlistened socket times out instead of refusing."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def lake(adapter_factory) -> Iterator[tuple[DuckDBAdapter, str]]:
    """A connected adapter and a fresh schema in the lake; the schema is dropped at the end."""
    adapter = adapter_factory()
    schema = f"pf_{uuid.uuid4().hex[:10]}"
    try:
        adapter.ensure_schema(schema)
        yield adapter, schema
    finally:
        adapter.drop_schema(schema)


@pytest.fixture
def refusing_catalog(lake_env, monkeypatch) -> int:
    """The lake's settings with the catalog address on a refusing port, and a short connect retry wait."""
    for key, value in lake_env.items():
        monkeypatch.setenv(key, value)
    port = _refusing_port()
    monkeypatch.setenv("DUCKDB_CATALOG_HOST", "127.0.0.1")
    monkeypatch.setenv("DUCKDB_CATALOG_PORT", str(port))
    monkeypatch.setattr(session_module, "_UNREACHABLE_RETRY_SECONDS", 0.01)
    return port


@pytest.fixture(params=["live_catalog", "refusing_catalog"])
def catalog(request, lake_env, monkeypatch) -> str:
    """The lake's catalog as the ops see it: the live one, or one behind a refusing port."""
    if request.param == "refusing_catalog":
        request.getfixturevalue("refusing_catalog")
    else:
        for key, value in lake_env.items():
            monkeypatch.setenv(key, value)
    return request.param


def _run(op: str, **env: str) -> tuple[int, dict]:
    out = io.StringIO()
    run = preflight.run_seed_preflight if op == "seed_preflight" else preflight.run_probe_views
    with contextlib.redirect_stdout(out):
        code = run({**os.environ, **env}, discover=lambda: ("duckdb", DuckDBAdapter))
    lines = out.getvalue().splitlines()
    assert lines[-3] == result.SENTINEL_BEGIN and lines[-1] == result.SENTINEL_END
    return code, json.loads(lines[-2])


def _seed(family: str, mode: str, schema: str = "analytics") -> tuple[int, dict]:
    return _run(
        "seed_preflight", SEED_PREFLIGHT_MODE=mode, SEED_PREFLIGHT_ENGINE_FAMILY=family,
        PROD_SCHEMA=schema, TABLE_NAME="seed",
    )


def _assert_nothing_was_listed(block: dict) -> None:
    assert block["checked"] is False and block["completeness"] == "none" and block["dependents"] == []
    assert block["drop_semantics"] == "late_binding"


def test_check_on_a_late_binding_lake_without_the_capability_passes(catalog):
    code, block = _seed("late_binding", "check")
    assert code == 0 and block["status"] == "success" and block["failures"] == 0
    assert "reason" not in block and block["mode"] == "check" and block["engine_family"] == "late_binding"
    _assert_nothing_was_listed(block)


def test_report_says_the_same(catalog):
    code, block = _seed("late_binding", "report")
    assert code == 0 and block["status"] == "success" and block["failures"] == 0
    assert "reason" not in block and block["mode"] == "report"
    _assert_nothing_was_listed(block)


@pytest.mark.parametrize("mode,code,status", [("check", 1, "error"), ("report", 0, "success")])
def test_a_schema_bound_release_on_duckdb_is_engine_mismatch(catalog, mode, code, status):
    got, block = _seed("schema_bound", mode)
    assert got == code and block["status"] == status
    assert block["reason"] == "engine_mismatch" and block["message"].startswith("engine_mismatch: ")
    _assert_nothing_was_listed(block)


def test_probe_reports_a_view_that_binds_and_one_that_no_longer_does(lake):
    """DuckLake binds a view when it is read, not when it is created.

    A view whose table is dropped fails its next read with a catalog error, and a
    view whose table is recreated without the column it reads fails with a binder
    error. Both are what the probe is meant to report.
    """
    adapter, s = lake
    for statement in (
        f'CREATE TABLE "{s}"."seed" (id integer)',
        f'CREATE VIEW "{s}"."ok" AS SELECT id FROM "{s}"."seed"',
        f'CREATE TABLE "{s}"."t2" (id integer)',
        f'CREATE VIEW "{s}"."dropped" AS SELECT id FROM "{s}"."t2"',
        f'DROP TABLE "{s}"."t2"',
        f'CREATE TABLE "{s}"."t3" (id integer)',
        f'CREATE VIEW "{s}"."replaced" AS SELECT id FROM "{s}"."t3"',
        f'DROP TABLE "{s}"."t3"',
        f'CREATE TABLE "{s}"."t3" (other varchar)',
    ):
        adapter.fetch(statement)
    code, block = _run("probe_views", PROBE_VIEWS=json.dumps([f"{s}.ok", f"{s}.dropped", f"{s}.replaced"]))
    assert code == 0 and block["status"] == "success" and block["failures"] == 0
    assert block["checked"] is True and "reason" not in block
    ok, dropped, replaced = block["views"]
    assert (ok["name"], ok["ok"], ok["error"]) == ("ok", True, "")
    assert (dropped["name"], dropped["ok"]) == ("dropped", False)
    assert "does not exist" in dropped["error"]
    assert (replaced["name"], replaced["ok"]) == ("replaced", False)
    assert 'column "id" not found' in replaced["error"]
    assert all("\n" not in v["error"] and len(v["error"]) <= 500 for v in block["views"])
    assert block["message"] == f"2 of 3 views failed: {s}.dropped, {s}.replaced"


def test_probe_of_an_unreachable_lake_never_fails(refusing_catalog):
    code, block = _run("probe_views", PROBE_VIEWS=json.dumps(["pf_none.v"]))
    assert code == 0 and block["status"] == "success" and block["failures"] == 0
    assert block["checked"] is False and block["reason"] == "warehouse_unreachable" and block["views"] == []
    assert block["message"].startswith("warehouse_unreachable: ")
