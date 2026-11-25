"""Unit tests for the dependents_check op: fake adapters, no warehouse."""
import contextlib
import io
import json
import logging

import pytest

from continuo_engine_contract import result
from continuo_engine_contract.port import AdapterDiscoveryError, Dependent, Dependents
from continuo_python_runtime.validation import dependents_check
from tests.warehouse_read_fakes import UNSET, adapter_class, run_op, with_env

CHECK = {
    "VALIDATION_OP": "dependents_check", "DEPENDENTS_CHECK_MODE": "check",
    "DEPENDENTS_CHECK_ENGINE_FAMILY": "schema_bound", "PROD_SCHEMA": "analytics",
    "TABLE_NAME": "seed_fx_rates_eur", "NODE_ID": "analytics.seed_fx_rates_eur",
    "DEPENDENTS_CHECK_ALLOWED": '["analytics.fx_rates_current", "analytics.fx_rates_latest_eur"]',
    "DEPENDENTS_CHECK_ACKNOWLEDGED": "[]",
}
# Two continuo views and one BI view read the seed.
SEED_DEPENDENTS = Dependents((
    Dependent("analytics", "fx_rates_current", "view", "dbt", 1),
    Dependent("analytics", "fx_rates_latest_eur", "view", "dbt", 2),
    Dependent("bi", "fx_rates_report", "view", "bi", 1),
), "exact")
LATE = {**CHECK, "DEPENDENTS_CHECK_ENGINE_FAMILY": "late_binding"}
BI_VIEW = Dependents((Dependent("bi", "fx_rates_report", "view", "bi", 1),), "best_effort")


def _seed(env, cls):
    return run_op(dependents_check.run_dependents_check, env, cls)


def test_check_stops_on_an_object_outside_the_allowed_set():
    code, block = _seed(CHECK, adapter_class("cascade", listing=SEED_DEPENDENTS))
    assert code == 1
    assert {k: v for k, v in block.items() if k != "message"} == {
        "schema_version": 1, "status": "error", "failures": 1, "unique_id": "analytics.seed_fx_rates_eur",
        "reason": "unowned_dependents", "op": "dependents_check", "mode": "check", "checked": True,
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
    cls = adapter_class("cascade", listing=SEED_DEPENDENTS)
    _seed(CHECK, cls)
    assert cls.state.listing_calls == [("analytics", "seed_fx_rates_eur")]
    assert cls.state.closed == 1


def test_check_passes_once_the_operator_acknowledges_the_object():
    env = with_env(CHECK, DEPENDENTS_CHECK_ACKNOWLEDGED='["bi.fx_rates_report"]')
    code, block = _seed(env, adapter_class("cascade", listing=SEED_DEPENDENTS))
    assert code == 0 and block["status"] == "success" and block["failures"] == 0
    assert "reason" not in block
    assert all(d["allowed"] for d in block["dependents"])


def test_report_never_fails_and_says_what_a_check_would():
    code, block = _seed(with_env(CHECK, DEPENDENTS_CHECK_MODE="report"), adapter_class("cascade", listing=SEED_DEPENDENTS))
    assert code == 0 and block["status"] == "success" and block["failures"] == 0
    assert block["reason"] == "unowned_dependents" and block["mode"] == "report"
    assert block["message"].startswith("unowned_dependents: ")


def test_late_binding_check_lists_external_views_and_passes():
    code, block = _seed(LATE, adapter_class("late_binding", listing=BI_VIEW))
    assert code == 0 and block["status"] == "success"
    assert "reason" not in block and block["completeness"] == "best_effort" and block["checked"] is True
    assert block["message"] == "1 external view(s) may go stale; recreate them after the reload: bi.fx_rates_report"
    assert [d["allowed"] for d in block["dependents"]] == [False]


@pytest.mark.parametrize("mode,code", [("check", 1), ("report", 0)])
def test_an_unexpected_family_is_engine_mismatch_before_connecting(mode, code):
    cls = adapter_class("late_binding", listing=SEED_DEPENDENTS)
    got, block = _seed(with_env(CHECK, DEPENDENTS_CHECK_MODE=mode), cls)
    assert got == code
    assert block["reason"] == "engine_mismatch" and block["checked"] is False
    assert block["completeness"] == "none" and block["dependents"] == []
    assert block["status"] == ("error" if mode == "check" else "success")
    assert "schema_bound" in block["message"] and "late_binding" in block["message"]
    assert cls.state.from_env_calls == 0


@pytest.mark.parametrize("mode,code", [("check", 1), ("report", 0)])
def test_an_adapter_that_declares_no_family_is_engine_mismatch(mode, code):
    cls = adapter_class(UNSET, listing=SEED_DEPENDENTS)
    got, block = _seed(with_env(CHECK, DEPENDENTS_CHECK_MODE=mode), cls)
    assert got == code and block["reason"] == "engine_mismatch" and block["drop_semantics"] == ""
    assert cls.state.from_env_calls == 0


@pytest.mark.parametrize("mode,code", [("check", 1), ("report", 0)])
def test_an_unreachable_warehouse_is_warehouse_unreachable(mode, code):
    cls = adapter_class("cascade", connect_error=ConnectionRefusedError("connection refused"))
    got, block = _seed(with_env(CHECK, DEPENDENTS_CHECK_MODE=mode), cls)
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
    cls = adapter_class(adapter_family, capability=False)
    got, block = _seed(env, cls)
    assert got == code and block.get("reason") == reason
    assert block["checked"] is False and block["completeness"] == "none"
    assert cls.state.from_env_calls == 0


@pytest.mark.parametrize("env,adapter_family,code", [(CHECK, "cascade", 1), (LATE, "late_binding", 0)])
def test_a_listing_failure(env, adapter_family, code):
    cls = adapter_class(adapter_family, listing_error=RuntimeError("permission denied"))
    got, block = _seed(env, cls)
    assert got == code and "reason" not in block
    assert block["status"] == ("error" if code else "success")
    assert block["checked"] is False and block["completeness"] == "none" and block["dependents"] == []
    assert "permission denied" in block["message"]
    assert cls.state.closed == 1


def test_a_seed_table_that_does_not_exist_yet_passes():
    code, block = _seed(CHECK, adapter_class("cascade", listing=Dependents((), "exact")))
    assert code == 0 and block["status"] == "success" and block["dependents"] == []
    assert block["checked"] is True and block["completeness"] == "exact"


def test_allowed_names_match_whatever_the_letter_case():
    env = with_env(CHECK, DEPENDENTS_CHECK_ALLOWED='["analytics.fx_rates_current"]')
    listing = Dependents((Dependent("Analytics", "FX_Rates_Current", "view", "dbt", 1),), "exact")
    code, block = _seed(env, adapter_class("cascade", listing=listing))
    assert code == 0 and block["dependents"][0]["allowed"] is True


def test_an_allowed_entry_is_matched_after_trimming_and_lower_casing():
    env = with_env(CHECK, DEPENDENTS_CHECK_ALLOWED='["  Analytics.FX_Rates_Current "]')
    listing = Dependents((Dependent("analytics", "fx_rates_current", "view", "dbt", 1),), "exact")
    code, block = _seed(env, adapter_class("cascade", listing=listing))
    assert code == 0 and block["dependents"][0]["allowed"] is True


def test_the_same_name_in_another_schema_is_not_allowed():
    env = with_env(CHECK, DEPENDENTS_CHECK_ALLOWED='["analytics.fx_rates_current"]')
    listing = Dependents((Dependent("bi", "fx_rates_current", "view", "bi", 1),), "exact")
    code, block = _seed(env, adapter_class("cascade", listing=listing))
    assert code == 1 and block["reason"] == "unowned_dependents"
    assert block["dependents"][0]["allowed"] is False


def test_duplicate_entries_in_the_allowed_and_acknowledged_lists_change_nothing():
    env = with_env(
        CHECK,
        DEPENDENTS_CHECK_ALLOWED='["analytics.fx_rates_current", "analytics.fx_rates_current", "ANALYTICS.fx_rates_current"]',
        DEPENDENTS_CHECK_ACKNOWLEDGED='["bi.fx_rates_report", "bi.fx_rates_report", "analytics.fx_rates_latest_eur"]',
    )
    code, block = _seed(env, adapter_class("cascade", listing=SEED_DEPENDENTS))
    assert code == 0 and all(d["allowed"] for d in block["dependents"])


def test_the_dependents_are_sorted_by_depth_then_schema_then_name():
    listing = Dependents((
        Dependent("b", "z", "view", "o", 2), Dependent("b", "a", "view", "o", 1),
        Dependent("a", "z", "view", "o", 1), Dependent("a", "a", "view", "o", 2),
    ), "exact")
    _, block = _seed(with_env(CHECK, DEPENDENTS_CHECK_MODE="report"), adapter_class("cascade", listing=listing))
    assert [(d["depth"], d["schema"], d["name"]) for d in block["dependents"]] == [
        (1, "a", "z"), (1, "b", "a"), (2, "a", "a"), (2, "b", "z"),
    ]


def test_the_node_id_defaults_to_the_production_relation():
    code, block = _seed(with_env(CHECK, NODE_ID=None), adapter_class("cascade", listing=Dependents((), "exact")))
    assert code == 0 and block["unique_id"] == "analytics.seed_fx_rates_eur"


def test_the_allowed_and_acknowledged_lists_default_to_empty():
    env = with_env(CHECK, DEPENDENTS_CHECK_ALLOWED=None, DEPENDENTS_CHECK_ACKNOWLEDGED=None)
    code, block = _seed(env, adapter_class("cascade", listing=SEED_DEPENDENTS))
    assert code == 1 and not any(d["allowed"] for d in block["dependents"])


@pytest.mark.parametrize("changes", [
    {"DEPENDENTS_CHECK_MODE": None},
    {"DEPENDENTS_CHECK_MODE": "drop"},
    {"PROD_SCHEMA": None},
    {"TABLE_NAME": None},
    {"DEPENDENTS_CHECK_ENGINE_FAMILY": None},
    {"DEPENDENTS_CHECK_ENGINE_FAMILY": "schema-bound"},
    {"DEPENDENTS_CHECK_ALLOWED": "not json"},
    {"DEPENDENTS_CHECK_ALLOWED": '{"a": 1}'},
    {"DEPENDENTS_CHECK_ALLOWED": "[1]"},
    {"DEPENDENTS_CHECK_ACKNOWLEDGED": '"bi.x"'},
])
def test_bad_inputs_exit_2(changes):
    cls = adapter_class("cascade", listing=SEED_DEPENDENTS)
    code, block = _seed(with_env(CHECK, **changes), cls)
    assert code == 2 and block["status"] == "error" and block["failures"] == 1
    assert block["op"] == "dependents_check" and "reason" not in block
    assert block["checked"] is False and block["completeness"] == "none" and block["dependents"] == []
    assert block["message"]
    assert cls.state.from_env_calls == 0


def test_a_discovery_failure_exits_2_in_both_modes():
    def discover():
        raise AdapterDiscoveryError("no engine adapter installed")

    for mode in ("check", "report"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dependents_check.run_dependents_check(with_env(CHECK, DEPENDENTS_CHECK_MODE=mode), discover=discover)
        block = json.loads(out.getvalue().splitlines()[-2])
        assert code == 2 and block["status"] == "error" and "reason" not in block
        assert "no engine adapter installed" in block["message"]


def test_missing_adapter_env_exits_2_before_connecting():
    cls = adapter_class("cascade", listing=SEED_DEPENDENTS, required_env=("POSTGRES_HOST",))
    code, block = _seed(CHECK, cls)
    assert code == 2 and "POSTGRES_HOST" in block["message"] and "reason" not in block
    assert cls.state.from_env_calls == 0


def test_adapter_env_is_read_from_the_given_env_not_the_process(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "from-the-process")
    cls = adapter_class("cascade", listing=SEED_DEPENDENTS, required_env=("POSTGRES_HOST",))
    code, _ = _seed(CHECK, cls)
    assert code == 2


# One row per line of the decision table, each in both modes:
# (adapter kwargs, env, check outcome, report outcome), an outcome being
# (exit code, status, reason, checked, completeness).
_BAD = (2, "error", "", False, "none")
_ROWS = {
    "bad input": (None, with_env(CHECK, DEPENDENTS_CHECK_MODE=None), None, None),
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
        ("cascade", {"listing": SEED_DEPENDENTS}), with_env(CHECK, DEPENDENTS_CHECK_ACKNOWLEDGED='["bi.fx_rates_report"]'),
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
    cls = adapter_class(adapter[0], **adapter[1]) if adapter else adapter_class("cascade", listing=SEED_DEPENDENTS)
    code, block = _seed(with_env(env, DEPENDENTS_CHECK_MODE=mode if row != "bad input" else None), cls)
    want = _BAD if adapter is None else (check if mode == "check" else report)
    got = (code, block["status"], block.get("reason", ""), block["checked"], block["completeness"])
    assert got == want
    assert block["failures"] == (1 if want[1] == "error" else 0)


def test_the_decision_function_is_pure():
    verdict = dependents_check.decide("check", "schema_bound", dependents_check.Situation.LISTED, detail="x", completeness="exact", has_unowned=True)
    assert verdict == dependents_check.decide("check", "schema_bound", dependents_check.Situation.LISTED, detail="x", completeness="exact", has_unowned=True)
    assert (verdict.exit_code, verdict.status, verdict.reason, verdict.checked, verdict.completeness) == (
        1, "error", "unowned_dependents", True, "exact")
    assert verdict.message == "unowned_dependents: x"


def test_an_unexpected_exception_still_prints_one_error_block(caplog):
    def discover():
        raise RuntimeError("boom")

    out = io.StringIO()
    with caplog.at_level(logging.ERROR, logger="validation_dependents_check"), contextlib.redirect_stdout(out):
        code = dependents_check.run_dependents_check(dict(CHECK), discover=discover)
    block = json.loads(out.getvalue().splitlines()[-2])
    assert out.getvalue().count(result.SENTINEL_BEGIN) == 1
    assert code == 2 and block["status"] == "error" and "boom" in block["message"]
    assert block["op"] == "dependents_check" and block["unique_id"] == "analytics.seed_fx_rates_eur"
    assert any(r.exc_info for r in caplog.records)


def test_an_unexpected_exception_after_connecting_closes_the_adapter_and_prints_one_block():
    cls = adapter_class("cascade", listing=object())  # no .objects
    code, block = _seed(CHECK, cls)
    assert code == 2 and block["status"] == "error" and block["message"]
    assert cls.state.closed == 1



def test_a_stop_is_logged_at_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="validation_dependents_check"):
        _seed(CHECK, adapter_class("cascade", listing=SEED_DEPENDENTS))
    texts = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("unowned_dependents" in t for t in texts)
