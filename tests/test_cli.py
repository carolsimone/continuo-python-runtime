"""Tests for CLI subcommands: validate, merge, hash, run."""

import json

import pyarrow as pa
import yaml

import continuo_python_runtime.cli as cli_module
from continuo_python_runtime.cli import main
from tests.conftest import FakeWarehouseAdapter


def test_validate_ok(contract_repo):
    assert main(["validate", str(contract_repo / "contracts")]) == 0


def test_validate_bad_dir_exits_1(tmp_path, caplog):
    assert main(["validate", str(tmp_path)]) == 1
    assert any("no contract files" in r.message for r in caplog.records)


def test_validate_dialect_flag_threads_to_loader(contract_repo, monkeypatch):
    captured = {}

    def fake_load_contract_dir(path, *, dialect=None):
        captured["dialect"] = dialect
        return []

    monkeypatch.setattr(cli_module, "load_contract_dir", fake_load_contract_dir)
    rc = main(["validate", str(contract_repo / "contracts"), "--dialect", "postgres"])
    assert rc == 0
    assert captured["dialect"] == "postgres"


def test_validate_dialect_defaults_to_none(contract_repo, monkeypatch):
    captured = {}

    def fake_load_contract_dir(path, *, dialect=None):
        captured["dialect"] = dialect
        return []

    monkeypatch.setattr(cli_module, "load_contract_dir", fake_load_contract_dir)
    rc = main(["validate", str(contract_repo / "contracts")])
    assert rc == 0
    assert captured["dialect"] is None


def test_validate_invalid_dialect_reports_bad_flag_not_bad_read(contract_repo, caplog):
    """End-to-end (no mocking of load_contract_dir): a typo'd --dialect value
    against a contract whose only read is perfectly valid SQL must exit 1
    with a message naming --dialect, not one that reads as though the SQL
    itself were rejected."""
    rc = main(["validate", str(contract_repo / "contracts"), "--dialect", "POSTGRES"])
    assert rc == 1
    messages = [r.message for r in caplog.records]
    assert any("--dialect" in m and "POSTGRES" in m for m in messages)
    assert not any("must be a single read query" in m for m in messages)


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


def _fake_build_wire_contract(captured):
    def fake(contract_dir, repo_root, service, *, dialect=None):
        captured["dialect"] = dialect
        return {"contract_version": 1, "service": service, "nodes": []}

    return fake


def test_merge_dialect_flag_threads_to_build_wire_contract(
    contract_repo, tmp_path, monkeypatch
):
    captured = {}
    monkeypatch.setattr(
        cli_module, "build_wire_contract", _fake_build_wire_contract(captured)
    )
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
            "--dialect",
            "trino",
        ]
    )
    assert rc == 0
    assert captured["dialect"] == "trino"


def test_merge_dialect_defaults_to_none(contract_repo, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        cli_module, "build_wire_contract", _fake_build_wire_contract(captured)
    )
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
    assert captured["dialect"] is None


def test_hash_prints_relation_and_hash(contract_repo, capsys):
    rc = main(
        ["hash", str(contract_repo / "contracts"), "--repo-root", str(contract_repo)]
    )
    assert rc == 0
    line = capsys.readouterr().out.strip()
    rel, h = line.split("\t")
    assert rel == "analytics.t" and h.startswith("sha256:")


def test_hash_dialect_flag_threads_to_build_wire_contract(contract_repo, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        cli_module, "build_wire_contract", _fake_build_wire_contract(captured)
    )
    rc = main(
        [
            "hash",
            str(contract_repo / "contracts"),
            "--repo-root",
            str(contract_repo),
            "--dialect",
            "postgres",
        ]
    )
    assert rc == 0
    assert captured["dialect"] == "postgres"


def test_hash_dialect_defaults_to_none(contract_repo, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        cli_module, "build_wire_contract", _fake_build_wire_contract(captured)
    )
    rc = main(
        ["hash", str(contract_repo / "contracts"), "--repo-root", str(contract_repo)]
    )
    assert rc == 0
    assert captured["dialect"] is None


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


def test_merge_creates_missing_out_dir(contract_repo, tmp_path):
    out = tmp_path / "nested" / "dir" / "contract.yaml"
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
    assert out.exists()


def test_lint_unreadable_file_reports_violation_and_exits_1(tmp_path, caplog):
    """A non-UTF-8 .py file should yield an 'unreadable' violation and exit 1
    instead of raising UnicodeDecodeError."""
    bad_file = tmp_path / "bad.py"
    bad_file.write_bytes(b"# caf\xe9\n")

    rc = main(["lint", str(bad_file)])
    assert rc == 1
    assert any("unreadable" in r.message for r in caplog.records)


def test_main_oserror_backstop_returns_1_no_traceback(tmp_path, caplog):
    """An OSError anywhere in a subcommand should be caught by the CLI
    backstop and turned into exit code 1, not a raw traceback."""
    # merge with a --repo-root that doesn't exist and a script that can't be
    # resolved should surface as a ContractError normally; to exercise the
    # OSError backstop directly, point --out at a path whose parent creation
    # is impossible (a file where a directory is expected).
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    out = blocker / "contract.yaml"

    from continuo_python_runtime.cli import main as cli_main

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "t.py").write_text("def run(ctx):\n    return ctx.read('ids')\n")
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    import yaml as _yaml

    (contracts_dir / "t.yml").write_text(_yaml.safe_dump({"nodes": [{
        "schema": "analytics", "table": "t", "owner": "m", "schedule": "daily",
        "criticality": "SECONDARY", "script": "scripts/t.py",
        "reads": {"ids": "select id from analytics.a"},
        "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
    }]}))

    rc = cli_main(
        [
            "merge",
            str(contracts_dir),
            "--service",
            "s",
            "--repo-root",
            str(tmp_path),
            "--out",
            str(out),
        ]
    )
    assert rc == 1
    assert any("blocker" in r.message or "Not a directory" in r.message or "Errno" in r.message for r in caplog.records)


def test_run_success_emits_sentinel_block(harness_repo, monkeypatch, capsys):
    """Test that run subcommand calls harness.run_node and returns its exit code."""
    # Set environment variables for run_node
    monkeypatch.setenv("NODE_ID", "python-model.svc.analytics.t")
    monkeypatch.setenv("TABLE_NAME", "t")
    monkeypatch.setenv("TARGET_SCHEMA", "analytics")
    monkeypatch.setenv("CONTRACT_DIR", str(harness_repo / "contracts"))
    monkeypatch.setenv("APP_ROOT", str(harness_repo))

    # Monkeypatch build_adapter to return FakeWarehouseAdapter
    def fake_build_adapter():
        return FakeWarehouseAdapter({"select id from analytics.a": pa.table({"id": [1]})})

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
