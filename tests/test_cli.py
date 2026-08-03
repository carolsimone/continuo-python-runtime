"""Tests for CLI subcommands: validate, merge, hash, run."""

import json

import pyarrow as pa
import yaml

from continuo_python_runtime.cli import main
from tests.conftest import FakeRuntimeAdapter


def test_validate_ok(contract_repo):
    assert main(["validate", str(contract_repo / "contracts")]) == 0


def test_validate_bad_dir_exits_1(tmp_path, caplog):
    assert main(["validate", str(tmp_path)]) == 1
    assert any("no contract files" in r.message for r in caplog.records)


def test_merge_writes_artifact(contract_repo, tmp_path):
    out = tmp_path / "contract.yaml"
    rc = main(
        [
            "merge",
            str(contract_repo / "contracts"),
            "--service",
            "s",
            "--repo-root",
            str(contract_repo),
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    assert yaml.safe_load(out.read_text())["service"] == "s"


def test_hash_prints_relation_and_hash(contract_repo, capsys):
    rc = main(
        ["hash", str(contract_repo / "contracts"), "--repo-root", str(contract_repo)]
    )
    assert rc == 0
    line = capsys.readouterr().out.strip()
    rel, h = line.split("\t")
    assert rel == "analytics.t" and h.startswith("sha256:")


def test_hash_missing_script_exits_1(contract_repo, caplog):
    # Modify contract to point to non-existent script
    contract_file = contract_repo / "contracts" / "t.yml"
    content = yaml.safe_load(contract_file.read_text())
    content["nodes"][0]["script"] = "scripts/nonexistent.py"
    contract_file.write_text(yaml.safe_dump(content))

    rc = main(
        ["hash", str(contract_repo / "contracts"), "--repo-root", str(contract_repo)]
    )
    assert rc == 1
    assert any("script not found" in r.message for r in caplog.records)


def test_lint_good_scripts_returns_0(tmp_path):
    # Create a good script directory
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "good.py").write_text("def run(ctx):\n    return ctx.read('ids')\n")

    rc = main(["lint", str(scripts_dir)])
    assert rc == 0


def test_lint_bad_scripts_returns_1(tmp_path, caplog):
    # Create a bad script directory with forbidden import
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "bad.py").write_text("import psycopg2\ndef run(ctx):\n    return None\n")

    rc = main(["lint", str(scripts_dir)])
    assert rc == 1
    assert any("psycopg2" in r.message for r in caplog.records)


def test_lint_nonexistent_path_returns_1(caplog):
    # Nonexistent path should exit 1 with path error, not crash
    rc = main(["lint", "/nonexistent/path/to/script.py"])
    assert rc == 1
    assert any("path does not exist" in r.message for r in caplog.records)


def test_run_success_emits_sentinel_block(harness_repo, monkeypatch, capsys):
    """Test that run subcommand calls harness.run_node and returns its exit code."""
    # Set environment variables for run_node
    monkeypatch.setenv("NODE_ID", "python-model.svc.analytics.t")
    monkeypatch.setenv("TABLE_NAME", "t")
    monkeypatch.setenv("TARGET_SCHEMA", "analytics")
    monkeypatch.setenv("CONTRACT_DIR", str(harness_repo / "contracts"))
    monkeypatch.setenv("APP_ROOT", str(harness_repo))

    # Monkeypatch build_adapter to return FakeRuntimeAdapter
    def fake_build_adapter():
        return FakeRuntimeAdapter({"select id from analytics.a": pa.table({"id": [1]})})

    import continuo_python_runtime.harness
    monkeypatch.setattr(continuo_python_runtime.harness, "build_adapter", fake_build_adapter)

    # Run the command
    rc = main(["run"])
    assert rc == 0

    # Check that exactly one sentinel block was emitted with "success"
    out = capsys.readouterr().out
    assert out.count("===CONTINUO_VALIDATION_RESULT_BEGIN===") == 1
    body = json.loads(out.split("BEGIN===\n")[1].split("\n===CONTINUO")[0])
    assert body["status"] == "success"
