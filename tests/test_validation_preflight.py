"""Unit tests for the seed_preflight and probe_views ops — fake adapters, no warehouse."""
import contextlib
import io
import json
import logging
from types import SimpleNamespace

import pytest

from continuo_engine_contract import result
from continuo_engine_contract.port import (
    AdapterDiscoveryError,
    Dependent,
    DependentsIntrospection,
    Dependents,
)
from continuo_python_runtime.validation import preflight

CHECK = {
    "VALIDATION_OP": "seed_preflight", "SEED_PREFLIGHT_MODE": "check",
    "SEED_PREFLIGHT_ENGINE_FAMILY": "schema_bound", "PROD_SCHEMA": "analytics",
    "TABLE_NAME": "seed_fx_rates_eur", "NODE_ID": "analytics.seed_fx_rates_eur",
    "SEED_PREFLIGHT_ALLOWED": '["analytics.fx_rates_current", "analytics.fx_rates_latest_eur"]',
    "SEED_PREFLIGHT_ACKNOWLEDGED": "[]",
}
# Two continuo views and one BI view read the seed.
SEED_DEPENDENTS = Dependents((
    Dependent("analytics", "fx_rates_current", "view", "dbt", 1),
    Dependent("analytics", "fx_rates_latest_eur", "view", "dbt", 2),
    Dependent("bi", "fx_rates_report", "view", "bi", 1),
), "exact")
LATE = {**CHECK, "SEED_PREFLIGHT_ENGINE_FAMILY": "late_binding"}
BI_VIEW = Dependents((Dependent("bi", "fx_rates_report", "view", "bi", 1),), "best_effort")
PROBE = {"VALIDATION_OP": "probe_views", "PROBE_VIEWS": '["bi.fx_rates_report", "bi.fx_rates_broken"]'}
UNSET = object()


def _adapter_class(drop_semantics, *, listing=None, capability=True, connect_error=None,
                   listing_error=None, broken_views=(), required_env=()):
    """A stand-in for the discovered adapter class; ``cls.state`` records what ran.

    ``drop_semantics=UNSET`` declares none. Without ``capability`` the class does
    not subclass ``DependentsIntrospection``.
    """
    state = SimpleNamespace(from_env_calls=0, listing_calls=[], fetched=[], closed=0)

    class _Adapter(*((DependentsIntrospection,) if capability else ())):
        @classmethod
        def required_env(cls):
            return list(required_env)

        @classmethod
        def from_env(cls):
            state.from_env_calls += 1
            if connect_error is not None:
                raise connect_error
            return cls()

        def dependents(self, schema, table):
            state.listing_calls.append((schema, table))
            if listing_error is not None:
                raise listing_error
            return listing

        def fetch(self, sql):
            state.fetched.append(sql)
            for view in broken_views:
                if f'."{view}" ' in sql:
                    raise RuntimeError(f"relation {view} does not exist")
            return None

        def close(self):
            state.closed += 1

    if drop_semantics is not UNSET:
        _Adapter.drop_semantics = drop_semantics
    _Adapter.state = state
    return _Adapter


def _run(env, cls, op="seed_preflight"):
    """Run an op with *env* and a fake discovery; return ``(exit_code, block)``.

    Asserts the block is the one and only block, the last line on stdout, and that
    its body parses as JSON.
    """
    out = io.StringIO()
    run = preflight.run_seed_preflight if op == "seed_preflight" else preflight.run_probe_views
    with contextlib.redirect_stdout(out):
        code = run(dict(env), discover=lambda: ("fake", cls))
    lines = out.getvalue().splitlines()
    assert out.getvalue().count(result.SENTINEL_BEGIN) == 1
    assert lines[-3] == result.SENTINEL_BEGIN and lines[-1] == result.SENTINEL_END
    return code, json.loads(lines[-2])


def _seed(env, cls):
    return _run(env, cls, "seed_preflight")


def _probe(env, cls):
    return _run(env, cls, "probe_views")


def _with(env, **changes):
    merged = {**env, **changes}
    return {k: v for k, v in merged.items() if v is not None}


def test_check_stops_on_an_object_outside_the_allowed_set():
    code, block = _seed(CHECK, _adapter_class("cascade", listing=SEED_DEPENDENTS))
    assert code == 1
    assert {k: v for k, v in block.items() if k != "message"} == {
        "schema_version": 1, "status": "error", "failures": 1, "unique_id": "analytics.seed_fx_rates_eur",
        "reason": "unowned_dependents", "op": "seed_preflight", "mode": "check", "checked": True,
        "engine_family": "schema_bound", "drop_semantics": "cascade", "completeness": "exact",
        "dependents": [
            {"schema": "analytics", "name": "fx_rates_current", "kind": "view", "owner": "dbt", "depth": 1, "allowed": True},
            {"schema": "bi", "name": "fx_rates_report", "kind": "view", "owner": "bi", "depth": 1, "allowed": False},
            {"schema": "analytics", "name": "fx_rates_latest_eur", "kind": "view", "owner": "dbt", "depth": 2, "allowed": True},
        ],
    }
    assert block["message"].startswith("unowned_dependents: ")
    assert "bi.fx_rates_report (view, owner bi)" in block["message"]
    assert "fx_rates_current" not in block["message"]


def test_the_listing_is_asked_for_the_production_relation_and_the_adapter_is_closed():
    cls = _adapter_class("cascade", listing=SEED_DEPENDENTS)
    _seed(CHECK, cls)
    assert cls.state.listing_calls == [("analytics", "seed_fx_rates_eur")]
    assert cls.state.closed == 1


def test_check_passes_once_the_operator_acknowledges_the_object():
    env = _with(CHECK, SEED_PREFLIGHT_ACKNOWLEDGED='["bi.fx_rates_report"]')
    code, block = _seed(env, _adapter_class("cascade", listing=SEED_DEPENDENTS))
    assert code == 0 and block["status"] == "success" and block["failures"] == 0
    assert "reason" not in block
    assert all(d["allowed"] for d in block["dependents"])


def test_report_never_fails_and_says_what_a_check_would():
    code, block = _seed(_with(CHECK, SEED_PREFLIGHT_MODE="report"), _adapter_class("cascade", listing=SEED_DEPENDENTS))
    assert code == 0 and block["status"] == "success" and block["failures"] == 0
    assert block["reason"] == "unowned_dependents" and block["mode"] == "report"
    assert block["message"].startswith("unowned_dependents: ")


def test_late_binding_check_lists_external_views_and_passes():
    code, block = _seed(LATE, _adapter_class("late_binding", listing=BI_VIEW))
    assert code == 0 and block["status"] == "success"
    assert "reason" not in block and block["completeness"] == "best_effort" and block["checked"] is True
    assert block["message"] == "1 external view(s) may go stale; recreate them after the reload: bi.fx_rates_report"
    assert [d["allowed"] for d in block["dependents"]] == [False]


@pytest.mark.parametrize("mode,code", [("check", 1), ("report", 0)])
def test_an_unexpected_family_is_engine_mismatch_before_connecting(mode, code):
    cls = _adapter_class("late_binding", listing=SEED_DEPENDENTS)
    got, block = _seed(_with(CHECK, SEED_PREFLIGHT_MODE=mode), cls)
    assert got == code
    assert block["reason"] == "engine_mismatch" and block["checked"] is False
    assert block["completeness"] == "none" and block["dependents"] == []
    assert block["status"] == ("error" if mode == "check" else "success")
    assert "schema_bound" in block["message"] and "late_binding" in block["message"]
    assert cls.state.from_env_calls == 0


@pytest.mark.parametrize("mode,code", [("check", 1), ("report", 0)])
def test_an_adapter_that_declares_no_family_is_engine_mismatch(mode, code):
    cls = _adapter_class(UNSET, listing=SEED_DEPENDENTS)
    got, block = _seed(_with(CHECK, SEED_PREFLIGHT_MODE=mode), cls)
    assert got == code and block["reason"] == "engine_mismatch" and block["drop_semantics"] == ""
    assert cls.state.from_env_calls == 0


@pytest.mark.parametrize("mode,code", [("check", 1), ("report", 0)])
def test_an_unreachable_warehouse_is_warehouse_unreachable(mode, code):
    cls = _adapter_class("cascade", connect_error=ConnectionRefusedError("connection refused"))
    got, block = _seed(_with(CHECK, SEED_PREFLIGHT_MODE=mode), cls)
    assert got == code
    assert block["status"] == ("error" if mode == "check" else "success")
    assert block["reason"] == "warehouse_unreachable" and block["checked"] is False
    assert block["completeness"] == "none" and block["dependents"] == []
    assert block["message"].startswith("warehouse_unreachable: ") and "connection refused" in block["message"]


@pytest.mark.parametrize("env,adapter_family,code,reason", [
    (CHECK, "cascade", 1, "unowned_dependents"),
    (LATE, "late_binding", 0, None),
])
def test_an_adapter_that_cannot_list_dependents(env, adapter_family, code, reason):
    cls = _adapter_class(adapter_family, capability=False)
    got, block = _seed(env, cls)
    assert got == code and block.get("reason") == reason
    assert block["checked"] is False and block["completeness"] == "none"
    assert cls.state.from_env_calls == 0


@pytest.mark.parametrize("env,adapter_family,code", [(CHECK, "cascade", 1), (LATE, "late_binding", 0)])
def test_a_listing_failure(env, adapter_family, code):
    cls = _adapter_class(adapter_family, listing_error=RuntimeError("permission denied"))
    got, block = _seed(env, cls)
    assert got == code and "reason" not in block
    assert block["status"] == ("error" if code else "success")
    assert block["checked"] is False and block["completeness"] == "none" and block["dependents"] == []
    assert "permission denied" in block["message"]
    assert cls.state.closed == 1


def test_a_seed_table_that_does_not_exist_yet_passes():
    code, block = _seed(CHECK, _adapter_class("cascade", listing=Dependents((), "exact")))
    assert code == 0 and block["status"] == "success" and block["dependents"] == []
    assert block["checked"] is True and block["completeness"] == "exact"


def test_allowed_names_match_whatever_the_letter_case():
    env = _with(CHECK, SEED_PREFLIGHT_ALLOWED='["analytics.fx_rates_current"]')
    listing = Dependents((Dependent("Analytics", "FX_Rates_Current", "view", "dbt", 1),), "exact")
    code, block = _seed(env, _adapter_class("cascade", listing=listing))
    assert code == 0 and block["dependents"][0]["allowed"] is True


def test_an_allowed_entry_is_matched_after_trimming_and_lower_casing():
    env = _with(CHECK, SEED_PREFLIGHT_ALLOWED='["  Analytics.FX_Rates_Current "]')
    listing = Dependents((Dependent("analytics", "fx_rates_current", "view", "dbt", 1),), "exact")
    code, block = _seed(env, _adapter_class("cascade", listing=listing))
    assert code == 0 and block["dependents"][0]["allowed"] is True


def test_the_same_name_in_another_schema_is_not_allowed():
    env = _with(CHECK, SEED_PREFLIGHT_ALLOWED='["analytics.fx_rates_current"]')
    listing = Dependents((Dependent("bi", "fx_rates_current", "view", "bi", 1),), "exact")
    code, block = _seed(env, _adapter_class("cascade", listing=listing))
    assert code == 1 and block["reason"] == "unowned_dependents"
    assert block["dependents"][0]["allowed"] is False


def test_duplicate_entries_in_the_allowed_and_acknowledged_lists_change_nothing():
    env = _with(
        CHECK,
        SEED_PREFLIGHT_ALLOWED='["analytics.fx_rates_current", "analytics.fx_rates_current", "ANALYTICS.fx_rates_current"]',
        SEED_PREFLIGHT_ACKNOWLEDGED='["bi.fx_rates_report", "bi.fx_rates_report", "analytics.fx_rates_latest_eur"]',
    )
    code, block = _seed(env, _adapter_class("cascade", listing=SEED_DEPENDENTS))
    assert code == 0 and all(d["allowed"] for d in block["dependents"])


def test_the_dependents_are_sorted_by_depth_then_schema_then_name():
    listing = Dependents((
        Dependent("b", "z", "view", "o", 2), Dependent("b", "a", "view", "o", 1),
        Dependent("a", "z", "view", "o", 1), Dependent("a", "a", "view", "o", 2),
    ), "exact")
    _, block = _seed(_with(CHECK, SEED_PREFLIGHT_MODE="report"), _adapter_class("cascade", listing=listing))
    assert [(d["depth"], d["schema"], d["name"]) for d in block["dependents"]] == [
        (1, "a", "z"), (1, "b", "a"), (2, "a", "a"), (2, "b", "z"),
    ]


def test_the_node_id_defaults_to_the_production_relation():
    code, block = _seed(_with(CHECK, NODE_ID=None), _adapter_class("cascade", listing=Dependents((), "exact")))
    assert code == 0 and block["unique_id"] == "analytics.seed_fx_rates_eur"


def test_the_allowed_and_acknowledged_lists_default_to_empty():
    env = _with(CHECK, SEED_PREFLIGHT_ALLOWED=None, SEED_PREFLIGHT_ACKNOWLEDGED=None)
    code, block = _seed(env, _adapter_class("cascade", listing=SEED_DEPENDENTS))
    assert code == 1 and not any(d["allowed"] for d in block["dependents"])


@pytest.mark.parametrize("changes", [
    {"SEED_PREFLIGHT_MODE": None},
    {"SEED_PREFLIGHT_MODE": "drop"},
    {"PROD_SCHEMA": None},
    {"TABLE_NAME": None},
    {"SEED_PREFLIGHT_ENGINE_FAMILY": None},
    {"SEED_PREFLIGHT_ENGINE_FAMILY": "schema-bound"},
    {"SEED_PREFLIGHT_ALLOWED": "not json"},
    {"SEED_PREFLIGHT_ALLOWED": '{"a": 1}'},
    {"SEED_PREFLIGHT_ALLOWED": "[1]"},
    {"SEED_PREFLIGHT_ACKNOWLEDGED": '"bi.x"'},
])
def test_bad_inputs_exit_2(changes):
    cls = _adapter_class("cascade", listing=SEED_DEPENDENTS)
    code, block = _seed(_with(CHECK, **changes), cls)
    assert code == 2 and block["status"] == "error" and block["failures"] == 1
    assert block["op"] == "seed_preflight" and "reason" not in block
    assert block["checked"] is False and block["completeness"] == "none" and block["dependents"] == []
    assert block["message"]
    assert cls.state.from_env_calls == 0


def test_a_discovery_failure_exits_2_in_both_modes():
    def discover():
        raise AdapterDiscoveryError("no engine adapter installed")

    for mode in ("check", "report"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = preflight.run_seed_preflight(_with(CHECK, SEED_PREFLIGHT_MODE=mode), discover=discover)
        block = json.loads(out.getvalue().splitlines()[-2])
        assert code == 2 and block["status"] == "error" and "reason" not in block
        assert "no engine adapter installed" in block["message"]


def test_missing_adapter_env_exits_2_before_connecting():
    cls = _adapter_class("cascade", listing=SEED_DEPENDENTS, required_env=("POSTGRES_HOST",))
    code, block = _seed(CHECK, cls)
    assert code == 2 and "POSTGRES_HOST" in block["message"] and "reason" not in block
    assert cls.state.from_env_calls == 0


def test_adapter_env_is_read_from_the_given_env_not_the_process(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "from-the-process")
    cls = _adapter_class("cascade", listing=SEED_DEPENDENTS, required_env=("POSTGRES_HOST",))
    code, _ = _seed(CHECK, cls)
    assert code == 2


# One row per line of the decision table, each in both modes:
# (adapter kwargs, env, check outcome, report outcome), an outcome being
# (exit code, status, reason, checked, completeness).
_BAD = (2, "error", "", False, "none")
_ROWS = {
    "bad input": (None, _with(CHECK, SEED_PREFLIGHT_MODE=None), None, None),
    "engine mismatch": (
        ("late_binding", {"listing": SEED_DEPENDENTS}), CHECK,
        (1, "error", "engine_mismatch", False, "none"), (0, "success", "engine_mismatch", False, "none")),
    "no declared family": (
        (UNSET, {"listing": SEED_DEPENDENTS}), CHECK,
        (1, "error", "engine_mismatch", False, "none"), (0, "success", "engine_mismatch", False, "none")),
    "no capability, schema_bound": (
        ("cascade", {"capability": False}), CHECK,
        (1, "error", "unowned_dependents", False, "none"), (0, "success", "unowned_dependents", False, "none")),
    "no capability, late_binding": (
        ("late_binding", {"capability": False}), LATE,
        (0, "success", "", False, "none"), (0, "success", "", False, "none")),
    "unreachable, schema_bound": (
        ("cascade", {"connect_error": OSError("down")}), CHECK,
        (1, "error", "warehouse_unreachable", False, "none"), (0, "success", "warehouse_unreachable", False, "none")),
    "unreachable, late_binding": (
        ("late_binding", {"connect_error": OSError("down")}), LATE,
        (1, "error", "warehouse_unreachable", False, "none"), (0, "success", "warehouse_unreachable", False, "none")),
    "listing fails, schema_bound": (
        ("cascade", {"listing_error": OSError("denied")}), CHECK,
        (1, "error", "", False, "none"), (0, "success", "", False, "none")),
    "listing fails, late_binding": (
        ("late_binding", {"listing_error": OSError("denied")}), LATE,
        (0, "success", "", False, "none"), (0, "success", "", False, "none")),
    "listed, schema_bound, not allowed": (
        ("cascade", {"listing": SEED_DEPENDENTS}), CHECK,
        (1, "error", "unowned_dependents", True, "exact"), (0, "success", "unowned_dependents", True, "exact")),
    "listed, schema_bound, all allowed": (
        ("cascade", {"listing": SEED_DEPENDENTS}), _with(CHECK, SEED_PREFLIGHT_ACKNOWLEDGED='["bi.fx_rates_report"]'),
        (0, "success", "", True, "exact"), (0, "success", "", True, "exact")),
    "listed, schema_bound, nothing": (
        ("cascade", {"listing": Dependents((), "exact")}), CHECK,
        (0, "success", "", True, "exact"), (0, "success", "", True, "exact")),
    "listed, late_binding, external views": (
        ("late_binding", {"listing": BI_VIEW}), LATE,
        (0, "success", "", True, "best_effort"), (0, "success", "", True, "best_effort")),
    "listed, late_binding, nothing": (
        ("late_binding", {"listing": Dependents((), "best_effort")}), LATE,
        (0, "success", "", True, "best_effort"), (0, "success", "", True, "best_effort")),
}


@pytest.mark.parametrize("mode", ["check", "report"])
@pytest.mark.parametrize("row", sorted(_ROWS))
def test_every_row_of_the_decision_table(row, mode):
    adapter, env, check, report = _ROWS[row]
    cls = _adapter_class(adapter[0], **adapter[1]) if adapter else _adapter_class("cascade", listing=SEED_DEPENDENTS)
    code, block = _seed(_with(env, SEED_PREFLIGHT_MODE=mode if row != "bad input" else None), cls)
    want = _BAD if adapter is None else (check if mode == "check" else report)
    got = (code, block["status"], block.get("reason", ""), block["checked"], block["completeness"])
    assert got == want
    assert block["failures"] == (1 if want[1] == "error" else 0)


def test_the_decision_function_is_pure():
    verdict = preflight.decide("check", "schema_bound", preflight.Situation.LISTED, detail="x", completeness="exact", has_unowned=True)
    assert verdict == preflight.decide("check", "schema_bound", preflight.Situation.LISTED, detail="x", completeness="exact", has_unowned=True)
    assert (verdict.exit_code, verdict.status, verdict.reason, verdict.checked, verdict.completeness) == (
        1, "error", "unowned_dependents", True, "exact")
    assert verdict.message == "unowned_dependents: x"


def test_an_unexpected_exception_still_prints_one_error_block(caplog):
    def discover():
        raise RuntimeError("boom")

    out = io.StringIO()
    with caplog.at_level(logging.ERROR, logger="validation_preflight"), contextlib.redirect_stdout(out):
        code = preflight.run_seed_preflight(dict(CHECK), discover=discover)
    block = json.loads(out.getvalue().splitlines()[-2])
    assert out.getvalue().count(result.SENTINEL_BEGIN) == 1
    assert code == 2 and block["status"] == "error" and "boom" in block["message"]
    assert block["op"] == "seed_preflight" and block["unique_id"] == "analytics.seed_fx_rates_eur"
    assert any(r.exc_info for r in caplog.records)


def test_an_unexpected_exception_after_connecting_closes_the_adapter_and_prints_one_block():
    cls = _adapter_class("cascade", listing=object())  # no .objects
    code, block = _seed(CHECK, cls)
    assert code == 2 and block["status"] == "error" and block["message"]
    assert cls.state.closed == 1


def test_an_unexpected_exception_in_probe_views_still_prints_one_error_block(caplog):
    def discover():
        raise RuntimeError("boom")

    out = io.StringIO()
    with caplog.at_level(logging.ERROR, logger="validation_preflight"), contextlib.redirect_stdout(out):
        code = preflight.run_probe_views(dict(PROBE), discover=discover)
    block = json.loads(out.getvalue().splitlines()[-2])
    assert out.getvalue().count(result.SENTINEL_BEGIN) == 1
    assert code == 2 and block["status"] == "error" and "boom" in block["message"]
    assert block["op"] == "probe_views" and block["unique_id"] == "probe_views"
    assert any(r.exc_info for r in caplog.records)


def test_a_stop_and_a_failed_probe_are_logged_at_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="validation_preflight"):
        _seed(CHECK, _adapter_class("cascade", listing=SEED_DEPENDENTS))
        _probe(PROBE, _adapter_class("cascade", broken_views=("fx_rates_broken",)))
    texts = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("unowned_dependents" in t for t in texts)
    assert any("bi.fx_rates_broken" in t for t in texts)


def test_probe_reports_each_view():
    cls = _adapter_class("cascade", broken_views=("fx_rates_broken",))
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
    code, block = _probe(PROBE, _adapter_class("cascade"))
    assert code == 0 and block["message"] == "" and all(v["ok"] for v in block["views"])


def test_probe_uses_the_node_id_when_given():
    _, block = _probe(_with(PROBE, NODE_ID="model.x.y"), _adapter_class("cascade"))
    assert block["unique_id"] == "model.x.y"


def test_probe_cuts_a_long_driver_error_to_its_first_line():
    class Boom(Exception):
        pass

    cls = _adapter_class("cascade")
    cls.fetch = lambda self, sql: (_ for _ in ()).throw(Boom("first line " + "x" * 800 + "\nsecond line"))
    _, block = _probe(_with(PROBE, PROBE_VIEWS='["bi.v"]'), cls)
    error = block["views"][0]["error"]
    assert len(error) == 500 and error.startswith("first line xxx") and "second" not in error


def test_probe_names_the_exception_type_when_the_driver_gives_no_text():
    cls = _adapter_class("cascade")
    cls.fetch = lambda self, sql: (_ for _ in ()).throw(KeyError())
    _, block = _probe(_with(PROBE, PROBE_VIEWS='["bi.v"]'), cls)
    assert block["views"][0] == {"schema": "bi", "name": "v", "ok": False, "error": "KeyError"}


def test_probe_splits_a_name_on_the_first_dot_only():
    cls = _adapter_class("cascade")
    _, block = _probe(_with(PROBE, PROBE_VIEWS='["bi.we.ird"]'), cls)
    assert block["views"][0]["schema"] == "bi" and block["views"][0]["name"] == "we.ird"
    assert cls.state.fetched == ['SELECT * FROM "bi"."we.ird" LIMIT 0']


def test_probe_doubles_double_quotes_inside_names():
    cls = _adapter_class("cascade")
    _probe(_with(PROBE, PROBE_VIEWS=json.dumps(['b"i.na"me'])), cls)
    assert cls.state.fetched == ['SELECT * FROM "b""i"."na""me" LIMIT 0']


def test_probe_asks_about_a_repeated_view_once():
    cls = _adapter_class("cascade")
    _, block = _probe(_with(PROBE, PROBE_VIEWS='["bi.v", "bi.v", "bi.w"]'), cls)
    assert [v["name"] for v in block["views"]] == ["v", "w"]
    assert len(cls.state.fetched) == 2


def test_probe_of_an_unreachable_warehouse_never_fails():
    cls = _adapter_class("cascade", connect_error=ConnectionRefusedError("connection refused"))
    code, block = _probe(PROBE, cls)
    assert code == 0 and block["status"] == "success" and block["failures"] == 0
    assert block["checked"] is False and block["reason"] == "warehouse_unreachable" and block["views"] == []
    assert "connection refused" in block["message"]


def test_probe_bad_input_exits_2():
    code, block = _probe(_with(PROBE, PROBE_VIEWS='"bi.x"'), _adapter_class("cascade"))
    assert code == 2 and block["status"] == "error" and block["op"] == "probe_views"
    assert block["checked"] is False and block["views"] == []


@pytest.mark.parametrize("views", [None, "not json", '["nodot"]', '["bi."]', '[".name"]', "[1]", '{"a": 1}'])
def test_probe_bad_views_exit_2(views):
    cls = _adapter_class("cascade")
    code, block = _probe(_with(PROBE, PROBE_VIEWS=views), cls)
    assert code == 2 and block["status"] == "error" and block["message"] and "reason" not in block
    assert cls.state.from_env_calls == 0


def test_probe_missing_adapter_env_exits_2():
    cls = _adapter_class("cascade", required_env=("TRINO_HOST",))
    code, block = _probe(PROBE, cls)
    assert code == 2 and "TRINO_HOST" in block["message"] and cls.state.from_env_calls == 0
