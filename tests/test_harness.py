"""Tests for the harness: script dispatch, sentinel envelope, error taxonomy."""

import json

import pyarrow as pa
import pytest
import yaml

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


def test_harness_passes_node_config_through_to_ensure_table(tmp_path, capsys):
    """The harness threads node.config to ensure_table unconditionally, as a keyword."""
    (tmp_path / "contracts").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "t.py").write_text("def run(ctx):\n    return ctx.read('ids')\n")
    (tmp_path / "contracts" / "t.yml").write_text(yaml.safe_dump({"nodes": [{
        "schema": "analytics", "table": "t", "owner": "m", "schedule": "daily",
        "criticality": "SECONDARY", "script": "scripts/t.py",
        "reads": {"ids": "select id from analytics.a"},
        "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
        "config": {"indexes": [{"columns": ["id"]}]},
    }]}))
    ad = FakeRuntimeAdapter({"select id from analytics.a": pa.table({"id": [1]})})
    assert run_node(_env(tmp_path), adapter=ad) == 0
    assert ad.ensured_config == {"indexes": [{"columns": ["id"]}]}


def test_ensure_table_value_error_surfaces_as_load_error(harness_repo, capsys):
    """A ValueError from an adapter's config validation surfaces as LoadError.

    This is the runtime fail-closed behavior: ensure_table's config rejection
    is a plain ValueError, and run_node's try/except around ensure_table/load
    converts any non-HarnessError into LoadError for the sentinel block.
    """
    class BadConfigAdapter(FakeRuntimeAdapter):
        def ensure_table(self, schema, table, columns, config=None):
            raise ValueError("unrecognized config key: 'sortkey'")

    ad = BadConfigAdapter({"select id from analytics.a": pa.table({"id": [1]})})
    assert run_node(_env(harness_repo), adapter=ad) == 1
    out = capsys.readouterr().out
    assert out.count("===CONTINUO_VALIDATION_RESULT_BEGIN===") == 1
    assert '"message":"LoadError:' in out
    assert "sortkey" in out


# --- in-repo import closure must be importable at run time ---
#
# CI folds the transitive in-repo import closure into `shared_code_hash`, so a
# node script is expected to import its shared helpers. `exec_module` alone
# puts neither the repo root nor the script's own directory on `sys.path`, so
# without the harness's explicit insertion every one of these scripts dies at
# import time in production — while passing under pytest, which adds the
# rootdir to `sys.path` itself. Both forms are covered because `closure.py`
# resolves names against exactly these two roots (repo_root, then the
# importing file's directory).


def test_script_can_import_sibling_helper(harness_repo, isolated_import_state, capsys):
    """`import helpers` — the script's own directory must be importable."""
    (harness_repo / "scripts" / "helpers.py").write_text(
        "def only_ids(table):\n    return table.select(['id'])\n"
    )
    (harness_repo / "scripts" / "t.py").write_text(
        "import helpers\n\n\ndef run(ctx):\n    return helpers.only_ids(ctx.read('ids'))\n"
    )
    ad = FakeRuntimeAdapter({"select id from analytics.a": pa.table({"id": [1, 2]})})
    assert run_node(_env(harness_repo), adapter=ad) == 0
    assert ad.loaded[2].num_rows == 2


def test_script_can_import_package_qualified_helper(
    harness_repo, isolated_import_state, capsys
):
    """`import scripts.helpers` — the repo root must be importable too."""
    (harness_repo / "scripts" / "__init__.py").write_text("")
    (harness_repo / "scripts" / "helpers.py").write_text(
        "def only_ids(table):\n    return table.select(['id'])\n"
    )
    (harness_repo / "scripts" / "t.py").write_text(
        "import scripts.helpers\n\n\n"
        "def run(ctx):\n    return scripts.helpers.only_ids(ctx.read('ids'))\n"
    )
    ad = FakeRuntimeAdapter({"select id from analytics.a": pa.table({"id": [1, 2]})})
    assert run_node(_env(harness_repo), adapter=ad) == 0
    assert ad.loaded[2].num_rows == 2


def test_script_can_import_helper_from_a_sibling_directory(
    harness_repo, isolated_import_state, capsys
):
    """A helper in a `lib/` package outside `scripts/` resolves via the repo root."""
    (harness_repo / "lib").mkdir()
    (harness_repo / "lib" / "__init__.py").write_text("")
    (harness_repo / "lib" / "shared.py").write_text(
        "def only_ids(table):\n    return table.select(['id'])\n"
    )
    (harness_repo / "scripts" / "t.py").write_text(
        "from lib.shared import only_ids\n\n\n"
        "def run(ctx):\n    return only_ids(ctx.read('ids'))\n"
    )
    ad = FakeRuntimeAdapter({"select id from analytics.a": pa.table({"id": [1, 2]})})
    assert run_node(_env(harness_repo), adapter=ad) == 0
    assert ad.loaded[2].num_rows == 2


def test_deferred_import_inside_run_still_resolves(
    harness_repo, isolated_import_state, capsys
):
    """The path entries survive past import time: `run()` may import lazily."""
    (harness_repo / "scripts" / "helpers.py").write_text(
        "def only_ids(table):\n    return table.select(['id'])\n"
    )
    (harness_repo / "scripts" / "t.py").write_text(
        "def run(ctx):\n"
        "    import helpers\n"
        "    return helpers.only_ids(ctx.read('ids'))\n"
    )
    ad = FakeRuntimeAdapter({"select id from analytics.a": pa.table({"id": [1, 2]})})
    assert run_node(_env(harness_repo), adapter=ad) == 0
    assert ad.loaded[2].num_rows == 2


def test_load_script_puts_repo_root_and_script_dir_on_sys_path(
    harness_repo, isolated_import_state
):
    """Both roots land at the front, repo root first — closure.py's own order."""
    import sys

    nodes = load_contract_dir(harness_repo / "contracts")
    load_script(nodes[0], harness_repo)
    assert sys.path[0] == str(harness_repo)
    assert sys.path[1] == str(harness_repo / "scripts")


def test_load_script_does_not_duplicate_sys_path_entries(
    harness_repo, isolated_import_state
):
    """Re-entry is idempotent: no unbounded sys.path growth."""
    import sys

    nodes = load_contract_dir(harness_repo / "contracts")
    load_script(nodes[0], harness_repo)
    after_first = list(sys.path)
    load_script(nodes[0], harness_repo)
    assert sys.path == after_first


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
