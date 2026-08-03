"""Tests for contract v1 merger."""

import yaml

import pytest

from continuo_python_runtime.contract.merge import build_wire_contract, write_wire_contract
from continuo_python_runtime.errors import ContractError


def test_wire_contract_shape_and_hash(contract_repo):
    repo = contract_repo
    doc = build_wire_contract(repo / "contracts", repo, "marketing-py")
    assert doc["contract_version"] == 1
    assert doc["service"] == "marketing-py"
    (entry,) = doc["nodes"]
    assert entry["content_hash"].startswith("sha256:")


def test_hash_stable_across_contract_reformatting(contract_repo):
    repo = contract_repo
    h1 = build_wire_contract(repo / "contracts", repo, "s")["nodes"][0]["content_hash"]
    text = (repo / "contracts" / "t.yml").read_text()
    (repo / "contracts" / "t.yml").write_text(text.replace("select id", "select   id"))
    h2 = build_wire_contract(repo / "contracts", repo, "s")["nodes"][0]["content_hash"]
    assert h1 == h2


def test_missing_script_rejected(contract_repo):
    repo = contract_repo
    (repo / "scripts" / "t.py").unlink()
    with pytest.raises(ContractError, match="scripts/t.py"):
        build_wire_contract(repo / "contracts", repo, "s")


def test_absolute_script_path_rejected(tmp_path):
    """Reject absolute script paths."""
    repo = tmp_path
    (repo / "contracts").mkdir()
    (repo / "scripts").mkdir()
    (repo / "scripts" / "t.py").write_text("def run(ctx):\n    return ctx.read('ids')\n")

    (repo / "contracts" / "t.yml").write_text(yaml.safe_dump({"nodes": [{
        "schema": "analytics", "table": "t", "owner": "m", "schedule": "daily",
        "criticality": "SECONDARY", "script": "/etc/passwd",
        "reads": {"ids": "select id from analytics.a"},
        "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
    }]}))

    with pytest.raises(ContractError, match="must be relative"):
        build_wire_contract(repo / "contracts", repo, "s")


def test_absolute_script_path_inside_repo_rejected(tmp_path):
    """Reject absolute script paths even when they are inside the repository."""
    repo = tmp_path
    (repo / "contracts").mkdir()
    (repo / "scripts").mkdir()
    (repo / "scripts" / "t.py").write_text("def run(ctx):\n    return ctx.read('ids')\n")

    # Use absolute path to the script file inside the repo
    script_abs_path = (repo / "scripts" / "t.py").resolve()

    (repo / "contracts" / "t.yml").write_text(yaml.safe_dump({"nodes": [{
        "schema": "analytics", "table": "t", "owner": "m", "schedule": "daily",
        "criticality": "SECONDARY", "script": str(script_abs_path),
        "reads": {"ids": "select id from analytics.a"},
        "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
    }]}))

    with pytest.raises(ContractError, match="must be relative"):
        build_wire_contract(repo / "contracts", repo, "s")


def test_parent_directory_escape_rejected(tmp_path):
    """Reject relative script paths with .. that escape the repository."""
    repo = tmp_path
    (repo / "contracts").mkdir()
    (repo / "scripts").mkdir()
    (repo / "scripts" / "t.py").write_text("def run(ctx):\n    return ctx.read('ids')\n")

    (repo / "contracts" / "t.yml").write_text(yaml.safe_dump({"nodes": [{
        "schema": "analytics", "table": "t", "owner": "m", "schedule": "daily",
        "criticality": "SECONDARY", "script": "../outside.py",
        "reads": {"ids": "select id from analytics.a"},
        "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
    }]}))

    with pytest.raises(ContractError, match="escapes"):
        build_wire_contract(repo / "contracts", repo, "s")


def test_script_path_pointing_at_directory_rejected(tmp_path):
    """Reject script paths pointing at directories."""
    repo = tmp_path
    (repo / "contracts").mkdir()
    (repo / "scripts").mkdir()
    (repo / "scripts" / "subdir").mkdir()

    (repo / "contracts" / "t.yml").write_text(yaml.safe_dump({"nodes": [{
        "schema": "analytics", "table": "t", "owner": "m", "schedule": "daily",
        "criticality": "SECONDARY", "script": "scripts/subdir",
        "reads": {"ids": "select id from analytics.a"},
        "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
    }]}))

    with pytest.raises(ContractError, match="script not found"):
        build_wire_contract(repo / "contracts", repo, "s")


def test_write_wire_contract_creates_missing_out_dir(contract_repo, tmp_path):
    """write_wire_contract should create --out's parent directory rather than
    crashing with FileNotFoundError."""
    doc = build_wire_contract(contract_repo / "contracts", contract_repo, "s")
    out = tmp_path / "does" / "not" / "exist" / "contract.yaml"
    write_wire_contract(doc, out)
    assert out.exists()
    assert yaml.safe_load(out.read_text())["service"] == "s"
