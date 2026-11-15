"""The harness's start check: the image must reproduce the released content_hash."""

import contextlib
import io
import json
import logging
from importlib import metadata

import pyarrow as pa
import pytest

from continuo_python_runtime.cli import main
from continuo_python_runtime.contract.loader import load_contract_dir
from continuo_python_runtime.hash_inputs import gather_hash_inputs
from continuo_python_runtime.harness import run_node
from continuo_python_runtime.hashing import hash_parts
from tests.conftest import NODE_T, FakeWarehouseAdapter
from tests.odcs_builder import write_contract

READ = "select id from analytics.a"


def _env(repo, **extra):
    return {"NODE_ID": "python-node.svc.analytics.t", "TABLE_NAME": "t", "TARGET_SCHEMA": "analytics",
            "CONTRACT_DIR": str(repo / "contracts"), "APP_ROOT": str(repo), **extra}


def _image_parts(repo) -> dict[str, str]:
    """The four hash fields of the repository's only node, from the files as they are now."""
    (node,) = load_contract_dir(repo / "contracts")
    inputs = gather_hash_inputs(node, repo)
    return hash_parts(node, inputs.primary, inputs.units)


def _released(repo) -> str:
    """The content_hash `continuo-runtime hash` prints for the repository's only node."""
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        assert main(["hash", str(repo / "contracts"), "--repo-root", str(repo)]) == 0
    (line,) = printed.getvalue().splitlines()
    return line.split("\t")[1]


def _block(out: str) -> dict:
    assert out.count("===CONTINUO_VALIDATION_RESULT_BEGIN===") == 1
    return json.loads(out.split("BEGIN===\n")[1].split("\n===CONTINUO")[0])


def _adapter():
    return FakeWarehouseAdapter({READ: pa.table({"id": [1]})})


def _declare_assets(repo, assets):
    write_contract(repo / "contracts", **{**NODE_T, "assets": assets})


def _change_the_read(repo):
    contract = repo / "contracts" / "t.odcs.yaml"
    assert READ in contract.read_text()
    contract.write_text(contract.read_text().replace(READ, "select id from analytics.b"))


def test_the_start_check_passes_on_an_odcs_image(harness_repo, capsys):
    assert run_node(_env(harness_repo, CONTENT_HASH=_released(harness_repo)), adapter=_adapter()) == 0
    assert "reason" not in _block(capsys.readouterr().out)


def _assert_fails_closed(repo, capsys, released):
    adapter = _adapter()
    assert run_node(_env(repo, CONTENT_HASH=released), adapter=adapter) == 1
    block = _block(capsys.readouterr().out)
    assert (block["status"], block["reason"], block["failures"]) == ("error", "image_contract_mismatch", 1)
    assert block["message"].startswith("ImageContractMismatch: ")
    assert adapter.ensured is None and adapter.loaded is None
    return block


def test_the_start_check_fails_on_a_changed_read(harness_repo, capsys):
    released = _released(harness_repo)
    _change_the_read(harness_repo)
    block = _assert_fails_closed(harness_repo, capsys, released)
    assert f"CONTENT_HASH {released} differs from the image's content_hash" in block["message"]
    assert "analytics.t" in block["message"]


def test_an_edited_script_fails_closed(harness_repo, capsys):
    released = _released(harness_repo)
    (harness_repo / "scripts" / "t.py").write_text("def run(ctx):\n    return ctx.read('ids')  # edited\n")
    _assert_fails_closed(harness_repo, capsys, released)


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
    image = _image_parts(harness_repo)
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
    write_contract(
        tmp_path / "contracts", schema="analytics", table="orders_csv", owner="m", schedule="daily",
        criticality="SECONDARY", kind="python-csv", reads={"csv": "s3://drops/orders.csv"},
        output_columns=[{"name": "order_id", "type": "INTEGER", "nullable": False}],
    )
    released = _released(tmp_path)
    contract = tmp_path / "contracts" / "orders_csv.odcs.yaml"
    contract.write_text(contract.read_text().replace("orders.csv", "orders_v2.csv"))
    env = {"NODE_ID": "python-csv.svc.analytics.orders_csv", "TABLE_NAME": "orders_csv",
           "TARGET_SCHEMA": "analytics", "CONTRACT_DIR": str(tmp_path / "contracts"),
           "APP_ROOT": str(tmp_path), "CONTENT_HASH": released}
    adapter = FakeWarehouseAdapter()
    assert run_node(env, adapter=adapter) == 1
    block = _block(capsys.readouterr().out)
    assert block["reason"] == "image_contract_mismatch"
    assert adapter.ensured is None
