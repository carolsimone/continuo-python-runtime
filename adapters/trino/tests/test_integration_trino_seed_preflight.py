"""``seed_preflight`` and ``probe_views`` against Trino + Iceberg (tests/smoke/trino-stack).

Run the stack first:
    docker compose -f tests/smoke/trino-stack/docker-compose.yml up -d --wait

Every fixture creates uniquely named schemas and drops them in teardown, whether
the test passed or not.
"""
import contextlib
import io
import json
import os
import uuid
from collections.abc import Iterator

import pytest

from continuo_engine_contract import result
from continuo_python_runtime.validation import preflight
from continuo_trino_adapter.adapter import TrinoAdapter

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def trino_env(monkeypatch):
    monkeypatch.setenv("TRINO_HOST", "localhost")
    monkeypatch.setenv("TRINO_PORT", os.environ.get("VR_IT_TRINO_PORT", "18080"))
    monkeypatch.setenv("TRINO_USER", "continuo")
    monkeypatch.setenv("TRINO_CATALOG", "iceberg")
    monkeypatch.setenv("TRINO_HTTP_SCHEME", "http")
    monkeypatch.delenv("TRINO_PASSWORD", raising=False)


@pytest.fixture
def warehouse(trino_env) -> Iterator[tuple[TrinoAdapter, str, str]]:
    """A connected adapter and two fresh schemas, ``s`` and ``other``."""
    adapter = TrinoAdapter.from_env()
    suffix = uuid.uuid4().hex[:10]
    s, other = f"pf_{suffix}", f"pfo_{suffix}"
    try:
        adapter.ensure_schema(s)
        adapter.ensure_schema(other)
        yield adapter, s, other
    finally:
        try:
            adapter.drop_schema(s)
            adapter.drop_schema(other)
        finally:
            adapter.close()


def _run(op, **env):
    out = io.StringIO()
    run = preflight.run_seed_preflight if op == "seed_preflight" else preflight.run_probe_views
    with contextlib.redirect_stdout(out):
        code = run({**os.environ, **env}, discover=lambda: ("trino", TrinoAdapter))
    lines = out.getvalue().splitlines()
    assert lines[-3] == result.SENTINEL_BEGIN and lines[-1] == result.SENTINEL_END
    return code, json.loads(lines[-2])


def _view(adapter, schema, name, source_schema, source):
    adapter._execute(
        f'CREATE VIEW "iceberg"."{schema}"."{name}" AS SELECT "id" FROM "iceberg"."{source_schema}"."{source}"'
    )


def test_check_lists_an_external_view_and_passes(warehouse):
    adapter, s, other = warehouse
    adapter._execute(f'CREATE TABLE "iceberg"."{s}"."seed" (id integer)')
    _view(adapter, s, "cv", s, "seed")
    _view(adapter, other, "ext", s, "seed")
    code, block = _run(
        "seed_preflight", SEED_PREFLIGHT_MODE="check", SEED_PREFLIGHT_ENGINE_FAMILY="late_binding",
        PROD_SCHEMA=s, TABLE_NAME="seed", SEED_PREFLIGHT_ALLOWED=json.dumps([f"{s}.cv"]),
    )
    assert code == 0 and block["status"] == "success" and "reason" not in block
    assert block["completeness"] == "best_effort" and block["checked"] is True
    assert block["drop_semantics"] == "late_binding"
    assert {(d["schema"], d["name"], d["allowed"]) for d in block["dependents"]} == {(s, "cv", True), (other, "ext", False)}
    assert f"{other}.ext" in block["message"] and "may go stale" in block["message"]


def test_a_schema_bound_release_on_trino_is_engine_mismatch(warehouse):
    _, s, _ = warehouse
    code, block = _run(
        "seed_preflight", SEED_PREFLIGHT_MODE="check", SEED_PREFLIGHT_ENGINE_FAMILY="schema_bound",
        PROD_SCHEMA=s, TABLE_NAME="seed",
    )
    assert code == 1 and block["reason"] == "engine_mismatch" and block["checked"] is False


def test_probe_names_a_view_that_no_longer_binds(warehouse):
    adapter, s, _ = warehouse
    adapter._execute(f'CREATE TABLE "iceberg"."{s}"."seed" (id integer)')
    adapter._execute(f'CREATE TABLE "iceberg"."{s}"."t2" (id integer)')
    _view(adapter, s, "ok", s, "seed")
    _view(adapter, s, "broken", s, "t2")
    adapter._execute(f'DROP TABLE "iceberg"."{s}"."t2"')
    code, block = _run("probe_views", PROBE_VIEWS=json.dumps([f"{s}.ok", f"{s}.broken"]))
    assert code == 0 and block["status"] == "success" and block["checked"] is True
    assert [(v["name"], v["ok"]) for v in block["views"]] == [("ok", True), ("broken", False)]
    assert block["views"][1]["error"] and "\n" not in block["views"][1]["error"]
    assert block["message"] == f"1 of 2 views failed: {s}.broken"
