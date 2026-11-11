"""The harness's start check: the image must reproduce the released content_hash."""

import json
import logging
from importlib import metadata

import pyarrow as pa
import pytest
import yaml

from continuo_python_runtime.contract.merge import build_wire_contract
from continuo_python_runtime.harness import run_node
from tests.conftest import FakeWarehouseAdapter

READ = "select id from analytics.a"


def _env(repo, **extra):
    return {"NODE_ID": "python-node.svc.analytics.t", "TABLE_NAME": "t", "TARGET_SCHEMA": "analytics",
            "CONTRACT_DIR": str(repo / "contracts"), "APP_ROOT": str(repo), **extra}


def _entry(repo) -> dict:
    return build_wire_contract(repo / "contracts", repo, "s")["nodes"][0]


def _released(repo) -> str:
    return _entry(repo)["content_hash"]


def _block(out: str) -> dict:
    assert out.count("===CONTINUO_VALIDATION_RESULT_BEGIN===") == 1
    return json.loads(out.split("BEGIN===\n")[1].split("\n===CONTINUO")[0])


def _adapter():
    return FakeWarehouseAdapter({READ: pa.table({"id": [1]})})


def _declare_assets(repo, assets):
    contract = repo / "contracts" / "t.yml"
    doc = yaml.safe_load(contract.read_text())
    doc["nodes"][0]["assets"] = assets
    contract.write_text(yaml.safe_dump(doc))


def test_a_matching_image_runs(harness_repo, capsys):
    assert run_node(_env(harness_repo, CONTENT_HASH=_released(harness_repo)), adapter=_adapter()) == 0
    assert "reason" not in _block(capsys.readouterr().out)


@pytest.mark.parametrize("edit", [
    lambda repo: (repo / "scripts" / "t.py").write_text("def run(ctx):\n    return ctx.read('ids')  # edited\n"),
    lambda repo: (repo / "contracts" / "t.yml").write_text(
        (repo / "contracts" / "t.yml").read_text().replace(READ, "select id from analytics.b")),
], ids=["script", "contract"])
def test_an_image_that_differs_from_the_release_fails_closed(harness_repo, capsys, edit):
    released = _released(harness_repo)
    edit(harness_repo)
    adapter = _adapter()
    assert run_node(_env(harness_repo, CONTENT_HASH=released), adapter=adapter) == 1
    block = _block(capsys.readouterr().out)
    assert (block["status"], block["reason"], block["failures"]) == ("error", "image_contract_mismatch", 1)
    assert block["message"].startswith("ImageContractMismatch: ")
    assert adapter.ensured is None and adapter.loaded is None


def test_an_edited_helper_module_fails_closed(harness_repo, capsys):
    (harness_repo / "scripts" / "helpers.py").write_text("RATE = 1\n")
    (harness_repo / "scripts" / "t.py").write_text("import helpers\n\n\ndef run(ctx):\n    return ctx.read('ids')\n")
    released = _released(harness_repo)
    (harness_repo / "scripts" / "helpers.py").write_text("RATE = 2\n")
    assert run_node(_env(harness_repo, CONTENT_HASH=released), adapter=_adapter()) == 1
    assert _block(capsys.readouterr().out)["reason"] == "image_contract_mismatch"


def test_an_edited_asset_fails_closed(harness_repo, capsys):
    (harness_repo / "data").mkdir()
    (harness_repo / "data" / "ref.json").write_text('{"rate": 1}')
    _declare_assets(harness_repo, ["data/ref.json"])
    released = _released(harness_repo)
    (harness_repo / "data" / "ref.json").write_text('{"rate": 2}')
    assert run_node(_env(harness_repo, CONTENT_HASH=released), adapter=_adapter()) == 1
    assert _block(capsys.readouterr().out)["reason"] == "image_contract_mismatch"


def test_an_asset_missing_from_the_image_is_a_mismatch(harness_repo, capsys):
    (harness_repo / "data").mkdir()
    (harness_repo / "data" / "ref.json").write_text('{"rate": 1}')
    _declare_assets(harness_repo, ["data/ref.json"])
    released = _released(harness_repo)
    (harness_repo / "data" / "ref.json").unlink()
    adapter = _adapter()
    assert run_node(_env(harness_repo, CONTENT_HASH=released), adapter=adapter) == 1
    block = _block(capsys.readouterr().out)
    assert block["reason"] == "image_contract_mismatch"
    assert block["message"].startswith("ImageContractMismatch: ")
    message = block["message"]
    assert "data/ref.json" in message
    assert "differs from cannot" not in message
    assert f"the image cannot reproduce CONTENT_HASH {released}" in message
    assert "analytics.t" in message
    assert f"runtime {metadata.version('continuo-python-runtime')}" in message
    assert "rebuild the image from the released commit" in message
    assert adapter.ensured is None


def test_mismatch_message_names_the_node_both_hashes_the_parts_and_the_runtime_version(harness_repo, capsys):
    (harness_repo / "scripts" / "helpers.py").write_text("RATE = 1\n")
    (harness_repo / "scripts" / "t.py").write_text("import helpers\n\n\ndef run(ctx):\n    return ctx.read('ids')\n")
    released = _released(harness_repo)
    (harness_repo / "scripts" / "t.py").write_text(
        "import helpers\n\n\ndef run(ctx):\n    return ctx.read('ids')  # edited\n")
    image = _entry(harness_repo)
    run_node(_env(harness_repo, CONTENT_HASH=released), adapter=_adapter())
    message = _block(capsys.readouterr().out)["message"]
    assert "analytics.t" in message
    assert f"CONTENT_HASH {released}" in message
    assert f"content_hash {image['content_hash']}" in message
    for part in ("source_hash", "shared_code_hash", "config_hash"):
        assert f"{part} {image[part]}" in message
    assert "units ['scripts/helpers.py']" in message
    assert f"runtime {metadata.version('continuo-python-runtime')}" in message
    assert "rebuild the image from the released commit" in message


@pytest.mark.parametrize("spelled", [
    lambda released: " ",
    lambda released: released.removeprefix("sha256:"),
    lambda released: released.upper().replace("SHA256:", "sha256:"),
    lambda released: released + "\n",
], ids=["whitespace", "no-prefix", "uppercase-hex", "trailing-newline"])
def test_a_content_hash_that_is_not_the_exact_string_fails_closed(harness_repo, capsys, spelled):
    released = _released(harness_repo)
    assert run_node(_env(harness_repo, CONTENT_HASH=spelled(released)), adapter=_adapter()) == 1
    assert _block(capsys.readouterr().out)["reason"] == "image_contract_mismatch"


def test_absent_content_hash_warns_and_runs(harness_repo, capsys, caplog):
    caplog.set_level(logging.WARNING)
    assert run_node(_env(harness_repo), adapter=_adapter()) == 0
    assert any(r.levelno == logging.WARNING and "CONTENT_HASH" in r.getMessage() for r in caplog.records)


def test_empty_content_hash_warns_and_runs(harness_repo, capsys, caplog):
    caplog.set_level(logging.WARNING)
    assert run_node(_env(harness_repo, CONTENT_HASH=""), adapter=_adapter()) == 0
    assert "reason" not in _block(capsys.readouterr().out)
    assert any("CONTENT_HASH" in r.getMessage() for r in caplog.records)


def test_a_matching_image_logs_info(harness_repo, caplog):
    caplog.set_level(logging.INFO, logger="continuo_python_runtime.content_check")
    assert run_node(_env(harness_repo, CONTENT_HASH=_released(harness_repo)), adapter=_adapter()) == 0
    assert any(r.levelno == logging.INFO and "analytics.t" in r.getMessage() for r in caplog.records)


def test_a_csv_node_is_checked_too(tmp_path, capsys):
    (tmp_path / "contracts").mkdir()
    contract = tmp_path / "contracts" / "t.yml"
    contract.write_text(yaml.safe_dump({"nodes": [{
        "schema": "analytics", "table": "orders_csv", "owner": "m", "schedule": "daily",
        "criticality": "SECONDARY", "kind": "python-csv",
        "reads": {"csv": "s3://drops/orders.csv"},
        "output_columns": [{"name": "order_id", "type": "INTEGER", "nullable": False}],
    }]}))
    released = build_wire_contract(tmp_path / "contracts", tmp_path, "s")["nodes"][0]["content_hash"]
    contract.write_text(contract.read_text().replace("orders.csv", "orders_v2.csv"))
    env = {"NODE_ID": "python-csv.svc.analytics.orders_csv", "TABLE_NAME": "orders_csv",
           "TARGET_SCHEMA": "analytics", "CONTRACT_DIR": str(tmp_path / "contracts"),
           "APP_ROOT": str(tmp_path), "CONTENT_HASH": released}
    adapter = FakeWarehouseAdapter()
    assert run_node(env, adapter=adapter) == 1
    block = _block(capsys.readouterr().out)
    assert block["reason"] == "image_contract_mismatch"
    assert adapter.ensured is None
