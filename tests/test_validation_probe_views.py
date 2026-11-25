"""Unit tests for the probe_views op: fake adapters, no warehouse."""
import contextlib
import io
import json
import logging

import pytest

from continuo_engine_contract import result
from continuo_python_runtime.validation import probe_views
from tests.warehouse_read_fakes import adapter_class, run_op, with_env

PROBE = {"VALIDATION_OP": "probe_views", "PROBE_VIEWS": '["bi.fx_rates_report", "bi.fx_rates_broken"]'}


def _probe(env, cls):
    return run_op(probe_views.run_probe_views, env, cls)


def test_an_unexpected_exception_in_probe_views_still_prints_one_error_block(caplog):
    def discover():
        raise RuntimeError("boom")

    out = io.StringIO()
    with caplog.at_level(logging.ERROR, logger="validation_probe_views"), contextlib.redirect_stdout(out):
        code = probe_views.run_probe_views(dict(PROBE), discover=discover)
    block = json.loads(out.getvalue().splitlines()[-2])
    assert out.getvalue().count(result.SENTINEL_BEGIN) == 1
    assert code == 2 and block["status"] == "error" and "boom" in block["message"]
    assert block["op"] == "probe_views" and block["unique_id"] == "probe_views"
    assert any(r.exc_info for r in caplog.records)


def test_a_failed_probe_is_logged_at_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="validation_probe_views"):
        _probe(PROBE, adapter_class("cascade", broken_views=("fx_rates_broken",)))
    texts = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("bi.fx_rates_broken" in t for t in texts)


def test_probe_reports_each_view():
    cls = adapter_class("cascade", broken_views=("fx_rates_broken",))
    code, block = _probe(PROBE, cls)
    assert code == 0 and block["status"] == "success" and block["failures"] == 0
    assert block["op"] == "probe_views" and block["checked"] is True and block["unique_id"] == "probe_views"
    assert "reason" not in block
    assert block["views"] == [
        {"schema": "bi", "name": "fx_rates_report", "ok": True, "error": ""},
        {"schema": "bi", "name": "fx_rates_broken", "ok": False, "error": "relation fx_rates_broken does not exist"},
    ]
    assert cls.state.fetched == [
        'SELECT * FROM "bi"."fx_rates_report" LIMIT 0',
        'SELECT * FROM "bi"."fx_rates_broken" LIMIT 0',
    ]
    assert block["message"] == "1 of 2 views failed: bi.fx_rates_broken"
    assert cls.state.closed == 1


def test_probe_with_every_view_binding_has_no_message():
    code, block = _probe(PROBE, adapter_class("cascade"))
    assert code == 0 and block["message"] == "" and all(v["ok"] for v in block["views"])


def test_probe_uses_the_node_id_when_given():
    _, block = _probe(with_env(PROBE, NODE_ID="model.x.y"), adapter_class("cascade"))
    assert block["unique_id"] == "model.x.y"


def test_probe_cuts_a_long_driver_error_to_its_first_line():
    class Boom(Exception):
        pass

    cls = adapter_class("cascade")
    cls.fetch = lambda self, sql: (_ for _ in ()).throw(Boom("first line " + "x" * 800 + "\nsecond line"))
    _, block = _probe(with_env(PROBE, PROBE_VIEWS='["bi.v"]'), cls)
    error = block["views"][0]["error"]
    assert len(error) == 500 and error.startswith("first line xxx") and "second" not in error


def test_probe_names_the_exception_type_when_the_driver_gives_no_text():
    cls = adapter_class("cascade")
    cls.fetch = lambda self, sql: (_ for _ in ()).throw(KeyError())
    _, block = _probe(with_env(PROBE, PROBE_VIEWS='["bi.v"]'), cls)
    assert block["views"][0] == {"schema": "bi", "name": "v", "ok": False, "error": "KeyError"}


def test_probe_splits_a_name_on_the_first_dot_only():
    cls = adapter_class("cascade")
    _, block = _probe(with_env(PROBE, PROBE_VIEWS='["bi.we.ird"]'), cls)
    assert block["views"][0]["schema"] == "bi" and block["views"][0]["name"] == "we.ird"
    assert cls.state.fetched == ['SELECT * FROM "bi"."we.ird" LIMIT 0']


def test_probe_doubles_double_quotes_inside_names():
    cls = adapter_class("cascade")
    _probe(with_env(PROBE, PROBE_VIEWS=json.dumps(['b"i.na"me'])), cls)
    assert cls.state.fetched == ['SELECT * FROM "b""i"."na""me" LIMIT 0']


def test_probe_asks_about_a_repeated_view_once():
    cls = adapter_class("cascade")
    _, block = _probe(with_env(PROBE, PROBE_VIEWS='["bi.v", "bi.v", "bi.w"]'), cls)
    assert [v["name"] for v in block["views"]] == ["v", "w"]
    assert len(cls.state.fetched) == 2


def test_probe_of_an_unreachable_warehouse_never_fails():
    cls = adapter_class("cascade", connect_error=ConnectionRefusedError("connection refused"))
    code, block = _probe(PROBE, cls)
    assert code == 0 and block["status"] == "success" and block["failures"] == 0
    assert block["checked"] is False and block["reason"] == "warehouse_unreachable" and block["views"] == []
    assert "connection refused" in block["message"]


def test_probe_bad_input_exits_2():
    code, block = _probe(with_env(PROBE, PROBE_VIEWS='"bi.x"'), adapter_class("cascade"))
    assert code == 2 and block["status"] == "error" and block["op"] == "probe_views"
    assert block["checked"] is False and block["views"] == []


@pytest.mark.parametrize("views", [None, "not json", '["nodot"]', '["bi."]', '[".name"]', "[1]", '{"a": 1}'])
def test_probe_bad_views_exit_2(views):
    cls = adapter_class("cascade")
    code, block = _probe(with_env(PROBE, PROBE_VIEWS=views), cls)
    assert code == 2 and block["status"] == "error" and block["message"] and "reason" not in block
    assert cls.state.from_env_calls == 0


def test_probe_missing_adapter_env_exits_2():
    cls = adapter_class("cascade", required_env=("TRINO_HOST",))
    code, block = _probe(PROBE, cls)
    assert code == 2 and "TRINO_HOST" in block["message"] and cls.state.from_env_calls == 0
