"""Tests for the harness: script dispatch, sentinel envelope, error taxonomy."""

import json

import pyarrow as pa
import pytest

from continuo_python_runtime.contract.loader import load_contract_dir
from continuo_python_runtime.errors import ContractError, ScriptError
from continuo_python_runtime.harness import load_script, run_node, select_node
from tests.conftest import FakeRuntimeAdapter


def _env(repo):
    return {
        "NODE_ID": "python-model.svc.analytics.t",
        "TABLE_NAME": "t",
        "TARGET_SCHEMA": "analytics",
        "CONTRACT_DIR": str(repo / "contracts"),
        "APP_ROOT": str(repo),
    }


def test_success_emits_single_sentinel_block(harness_repo, capsys):
    ad = FakeRuntimeAdapter({"select id from analytics.a": pa.table({"id": [1, 2]})})
    assert run_node(_env(harness_repo), adapter=ad) == 0
    out = capsys.readouterr().out
    assert out.count("===CONTINUO_VALIDATION_RESULT_BEGIN===") == 1
    body = json.loads(out.split("BEGIN===\n")[1].split("\n===CONTINUO")[0])
    assert body["status"] == "success"
    assert ad.loaded[0:2] == ("analytics", "t")
    assert ad.ensured is not None


def test_script_print_cannot_corrupt_stdout(harness_repo, capsys):
    (harness_repo / "scripts" / "t.py").write_text(
        "def run(ctx):\n    print('noise')\n    return ctx.read('ids')\n"
    )
    ad = FakeRuntimeAdapter({"select id from analytics.a": pa.table({"id": [1]})})
    run_node(_env(harness_repo), adapter=ad)
    out = capsys.readouterr().out
    assert "noise" not in out


def test_unknown_node_id_is_contract_error(harness_repo, capsys):
    env = _env(harness_repo) | {"NODE_ID": "python-model.svc.analytics.nope"}
    assert run_node(env, adapter=FakeRuntimeAdapter()) == 1
    assert '"message":"ContractError:' in capsys.readouterr().out


def test_conform_violation_is_conform_error(harness_repo, capsys):
    ad = FakeRuntimeAdapter({"select id from analytics.a": pa.table({"wrong": [1]})})
    assert run_node(_env(harness_repo), adapter=ad) == 1
    assert '"message":"ConformError:' in capsys.readouterr().out
    assert ad.loaded is None


def test_script_exception_is_script_error(harness_repo, capsys):
    (harness_repo / "scripts" / "t.py").write_text("def run(ctx):\n    raise ValueError('x')\n")
    assert run_node(_env(harness_repo), adapter=FakeRuntimeAdapter()) == 1
    assert '"message":"ScriptError:' in capsys.readouterr().out


def test_load_failure_is_load_error(harness_repo, capsys):
    class BadLoad(FakeRuntimeAdapter):
        def load(self, schema, table, data):
            raise RuntimeError("disk full")

    ad = BadLoad({"select id from analytics.a": pa.table({"id": [1]})})
    assert run_node(_env(harness_repo), adapter=ad) == 1
    assert '"message":"LoadError:' in capsys.readouterr().out


# --- additional coverage for the clarifications ---


def test_missing_required_env_key_is_contract_error(harness_repo, capsys):
    env = _env(harness_repo)
    del env["TABLE_NAME"]
    assert run_node(env, adapter=FakeRuntimeAdapter()) == 1
    out = capsys.readouterr().out
    assert '"message":"ContractError:' in out
    assert out.count("===CONTINUO_VALIDATION_RESULT_BEGIN===") == 1


def test_undeclared_read_is_read_error_not_script_error(harness_repo, capsys):
    (harness_repo / "scripts" / "t.py").write_text(
        "def run(ctx):\n    return ctx.read('nope')\n"
    )
    assert run_node(_env(harness_repo), adapter=FakeRuntimeAdapter()) == 1
    out = capsys.readouterr().out
    assert '"message":"ReadError:' in out


def test_select_node_matches_trailing_two_segments(harness_repo):
    nodes = load_contract_dir(harness_repo / "contracts")
    node = select_node(nodes, "anything.here.analytics.t")
    assert node.schema == "analytics"
    assert node.table == "t"


def test_select_node_too_few_segments_is_contract_error(harness_repo):
    nodes = load_contract_dir(harness_repo / "contracts")
    with pytest.raises(ContractError):
        select_node(nodes, "t")


def test_select_node_unknown_lists_available(harness_repo):
    nodes = load_contract_dir(harness_repo / "contracts")
    with pytest.raises(ContractError, match="analytics.t"):
        select_node(nodes, "x.y.nope.nope")


def test_load_script_missing_file_is_contract_error(harness_repo):
    nodes = load_contract_dir(harness_repo / "contracts")
    node = nodes[0]
    (harness_repo / "scripts" / "t.py").unlink()
    with pytest.raises(ContractError):
        load_script(node, harness_repo)


def test_load_script_without_run_is_script_error(harness_repo):
    nodes = load_contract_dir(harness_repo / "contracts")
    node = nodes[0]
    (harness_repo / "scripts" / "t.py").write_text("x = 1\n")
    with pytest.raises(ScriptError):
        load_script(node, harness_repo)


def test_load_script_rejects_absolute_path(harness_repo, node_fixture):
    import dataclasses

    node = dataclasses.replace(node_fixture, script=str(harness_repo / "scripts" / "t.py"))
    with pytest.raises(ContractError):
        load_script(node, harness_repo)


def test_app_root_defaults_to_contract_dir_parent(harness_repo, capsys):
    env = _env(harness_repo)
    del env["APP_ROOT"]
    ad = FakeRuntimeAdapter({"select id from analytics.a": pa.table({"id": [1]})})
    assert run_node(env, adapter=ad) == 0


def test_build_adapter_uses_discovery_seam(monkeypatch, harness_repo):
    import continuo_python_runtime.harness as harness_mod

    class DummyAdapter(FakeRuntimeAdapter):
        @classmethod
        def from_env(cls):
            return cls({"select id from analytics.a": pa.table({"id": [1]})})

    monkeypatch.setattr(
        harness_mod,
        "discover_runtime_adapter",
        lambda: ("dummy", DummyAdapter),
    )
    assert run_node(_env(harness_repo)) == 0


def test_script_import_syntax_error_emits_single_sentinel_block(harness_repo, capsys):
    (harness_repo / "scripts" / "t.py").write_text("def run(ctx:\n    return 1\n")
    assert run_node(_env(harness_repo), adapter=FakeRuntimeAdapter()) == 1
    out = capsys.readouterr().out
    assert out.count("===CONTINUO_VALIDATION_RESULT_BEGIN===") == 1
    body = json.loads(out.split("BEGIN===\n")[1].split("\n===CONTINUO")[0])
    assert body["message"].startswith("ScriptError:")


def test_script_import_time_exception_emits_single_sentinel_block(harness_repo, capsys):
    (harness_repo / "scripts" / "t.py").write_text(
        "raise RuntimeError('boom at import')\n"
    )
    assert run_node(_env(harness_repo), adapter=FakeRuntimeAdapter()) == 1
    out = capsys.readouterr().out
    assert out.count("===CONTINUO_VALIDATION_RESULT_BEGIN===") == 1
    body = json.loads(out.split("BEGIN===\n")[1].split("\n===CONTINUO")[0])
    assert body["message"].startswith("ScriptError:")


def test_unexpected_exception_still_emits_single_sentinel_block(
    monkeypatch, harness_repo, capsys
):
    import continuo_python_runtime.harness as harness_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("totally unexpected")

    monkeypatch.setattr(harness_mod, "conform", _boom)
    ad = FakeRuntimeAdapter({"select id from analytics.a": pa.table({"id": [1]})})
    assert run_node(_env(harness_repo), adapter=ad) == 1
    out = capsys.readouterr().out
    assert out.count("===CONTINUO_VALIDATION_RESULT_BEGIN===") == 1
    body = json.loads(out.split("BEGIN===\n")[1].split("\n===CONTINUO")[0])
    assert body["message"].startswith("ScriptError: unexpected failure:")


def test_module_level_print_does_not_reach_stdout(harness_repo, capsys):
    (harness_repo / "scripts" / "t.py").write_text(
        "print('module-level noise')\n\n\ndef run(ctx):\n    return ctx.read('ids')\n"
    )
    ad = FakeRuntimeAdapter({"select id from analytics.a": pa.table({"id": [1]})})
    run_node(_env(harness_repo), adapter=ad)
    out = capsys.readouterr().out
    assert "module-level noise" not in out


def test_missing_required_warehouse_env_is_load_error(monkeypatch, harness_repo, capsys):
    import continuo_python_runtime.harness as harness_mod

    class DummyAdapter(FakeRuntimeAdapter):
        @classmethod
        def required_env(cls):
            return ["WAREHOUSE_HOST", "WAREHOUSE_PASSWORD"]

        @classmethod
        def from_env(cls):
            return cls()

    monkeypatch.setattr(
        harness_mod, "discover_runtime_adapter", lambda: ("dummy", DummyAdapter)
    )
    monkeypatch.delenv("WAREHOUSE_HOST", raising=False)
    monkeypatch.delenv("WAREHOUSE_PASSWORD", raising=False)

    assert run_node(_env(harness_repo)) == 1
    out = capsys.readouterr().out
    assert out.count("===CONTINUO_VALIDATION_RESULT_BEGIN===") == 1
    body = json.loads(out.split("BEGIN===\n")[1].split("\n===CONTINUO")[0])
    assert body["message"].startswith("LoadError:")
    assert "WAREHOUSE_HOST" in body["message"]
    assert "WAREHOUSE_PASSWORD" in body["message"]


def test_adapter_close_called_on_success(harness_repo, capsys):
    ad = FakeRuntimeAdapter({"select id from analytics.a": pa.table({"id": [1]})})
    assert run_node(_env(harness_repo), adapter=ad) == 0
    assert ad.closed is True


def test_adapter_close_called_on_failure(harness_repo, capsys):
    ad = FakeRuntimeAdapter({"select id from analytics.a": pa.table({"wrong": [1]})})
    assert run_node(_env(harness_repo), adapter=ad) == 1
    assert ad.closed is True


def test_adapter_construction_failure_emits_single_load_error_block(monkeypatch, harness_repo, capsys):
    import continuo_python_runtime.harness as harness_mod

    class BoomAdapterDiscoveryError(Exception):
        pass

    def _raise():
        raise BoomAdapterDiscoveryError("no adapter installed")

    monkeypatch.setattr(harness_mod, "build_adapter", _raise)

    assert run_node(_env(harness_repo)) == 1
    out = capsys.readouterr().out
    assert out.count("===CONTINUO_VALIDATION_RESULT_BEGIN===") == 1
    assert '"message":"LoadError:' in out
