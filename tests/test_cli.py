"""Tests for CLI subcommands: validate, merge, hash."""

import yaml

from continuo_python_runtime.cli import main


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
