"""Tests for contract v1 merger."""

import yaml

import pytest

from continuo_python_runtime.contract.merge import build_wire_contract, write_wire_contract
from continuo_python_runtime.errors import ContractError
from continuo_python_runtime.hashing import content_hash_fold

WIRE_ENTRY_KEYS = {
    "schema", "table", "owner", "schedule", "criticality", "script", "reads",
    "output_columns", "description", "extra_columns", "config",
    "source_hash", "shared_code_hash", "config_hash", "content_hash",
}


def test_wire_contract_shape_and_hash(contract_repo):
    repo = contract_repo
    doc = build_wire_contract(repo / "contracts", repo, "marketing-py")
    assert doc["contract_version"] == 1
    assert doc["service"] == "marketing-py"
    (entry,) = doc["nodes"]
    assert entry["content_hash"].startswith("sha256:")


def test_wire_entry_has_exactly_the_wire_key_set(contract_repo):
    """An extra or missing key here is exactly what makes
    parse_python_contract reject the whole artifact on the other side."""
    repo = contract_repo
    (entry,) = build_wire_contract(repo / "contracts", repo, "s")["nodes"]
    assert set(entry) == WIRE_ENTRY_KEYS


def test_content_hash_prefixed_parts_are_bare(contract_repo):
    repo = contract_repo
    (entry,) = build_wire_contract(repo / "contracts", repo, "s")["nodes"]
    assert entry["content_hash"].startswith("sha256:")
    assert not entry["source_hash"].startswith("sha256:")
    assert not entry["shared_code_hash"].startswith("sha256:")
    assert not entry["config_hash"].startswith("sha256:")


def test_content_hash_equals_fold_of_entrys_own_parts(contract_repo):
    """The exact check manifest-controller re-runs on its side."""
    repo = contract_repo
    (entry,) = build_wire_contract(repo / "contracts", repo, "s")["nodes"]
    assert entry["content_hash"] == content_hash_fold(
        entry["source_hash"], entry["shared_code_hash"], entry["config_hash"]
    )


def test_node_with_no_in_repo_imports_has_empty_shared_code_hash(contract_repo):
    repo = contract_repo
    (entry,) = build_wire_contract(repo / "contracts", repo, "s")["nodes"]
    assert entry["shared_code_hash"] == ""


def test_editing_shared_helper_flips_only_the_importing_nodes_content_hash(tmp_path):
    """The 'flips exactly the nodes that reach it' invariant: two nodes, one
    imports a shared helper and one does not. Editing one byte of the helper
    changes the importing node's content_hash and leaves the other's alone."""
    repo = tmp_path
    (repo / "contracts").mkdir()
    (repo / "scripts").mkdir()
    (repo / "scripts" / "helpers.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "scripts" / "with_helper.py").write_text(
        "import scripts.helpers\n\n\ndef run(ctx):\n    return ctx.read('ids')\n"
    )
    (repo / "scripts" / "without_helper.py").write_text(
        "def run(ctx):\n    return ctx.read('ids')\n"
    )
    (repo / "contracts" / "nodes.yml").write_text(yaml.safe_dump({"nodes": [
        {
            "schema": "analytics", "table": "with_helper", "owner": "m",
            "schedule": "daily", "criticality": "SECONDARY",
            "script": "scripts/with_helper.py",
            "reads": {"ids": "select id from analytics.a"},
            "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
        },
        {
            "schema": "analytics", "table": "without_helper", "owner": "m",
            "schedule": "daily", "criticality": "SECONDARY",
            "script": "scripts/without_helper.py",
            "reads": {"ids": "select id from analytics.a"},
            "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
        },
    ]}))

    def hashes_by_table():
        doc = build_wire_contract(repo / "contracts", repo, "s")
        return {e["table"]: e for e in doc["nodes"]}

    before = hashes_by_table()
    assert before["with_helper"]["shared_code_hash"] != ""
    assert before["without_helper"]["shared_code_hash"] == ""

    helper_text = (repo / "scripts" / "helpers.py").read_text()
    (repo / "scripts" / "helpers.py").write_text(helper_text.replace("a + b", "a + b "))

    after = hashes_by_table()
    assert after["with_helper"]["content_hash"] != before["with_helper"]["content_hash"]
    assert after["without_helper"]["content_hash"] == before["without_helper"]["content_hash"]


def test_hash_stable_across_contract_reformatting(contract_repo):
    repo = contract_repo
    h1 = build_wire_contract(repo / "contracts", repo, "s")["nodes"][0]["content_hash"]
    text = (repo / "contracts" / "t.yml").read_text()
    (repo / "contracts" / "t.yml").write_text(text.replace("select id", "select   id"))
    h2 = build_wire_contract(repo / "contracts", repo, "s")["nodes"][0]["content_hash"]
    assert h1 == h2


def test_config_change_flips_content_hash_via_config_hash(contract_repo):
    repo = contract_repo
    doc1 = build_wire_contract(repo / "contracts", repo, "s")
    entry1 = doc1["nodes"][0]
    assert entry1["config"] == {}

    text = (repo / "contracts" / "t.yml").read_text()
    parsed = yaml.safe_load(text)
    parsed["nodes"][0]["config"] = {"indexes": [{"columns": ["id"], "unique": True}]}
    (repo / "contracts" / "t.yml").write_text(yaml.safe_dump(parsed))

    entry2 = build_wire_contract(repo / "contracts", repo, "s")["nodes"][0]
    assert entry2["config"] == {"indexes": [{"columns": ["id"], "unique": True}]}
    assert entry2["config_hash"] != entry1["config_hash"]
    assert entry2["content_hash"] != entry1["content_hash"]


def test_dynamic_import_in_script_raises_contract_error(tmp_path):
    repo = tmp_path
    (repo / "contracts").mkdir()
    (repo / "scripts").mkdir()
    (repo / "scripts" / "t.py").write_text(
        "def run(ctx):\n    exec(\"pass\")\n    return ctx.read('ids')\n"
    )
    (repo / "contracts" / "t.yml").write_text(yaml.safe_dump({"nodes": [{
        "schema": "analytics", "table": "t", "owner": "m", "schedule": "daily",
        "criticality": "SECONDARY", "script": "scripts/t.py",
        "reads": {"ids": "select id from analytics.a"},
        "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
    }]}))

    with pytest.raises(ContractError):
        build_wire_contract(repo / "contracts", repo, "s")


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


def _write_single_node_contract(repo, script: str):
    (repo / "contracts").mkdir()
    (repo / "contracts" / "t.yml").write_text(yaml.safe_dump({"nodes": [{
        "schema": "analytics", "table": "t", "owner": "m", "schedule": "daily",
        "criticality": "SECONDARY", "script": script,
        "reads": {"ids": "select id from analytics.a"},
        "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
    }]}))


def test_closure_member_forbidden_driver_import_raises(tmp_path):
    """review-fix-b reproduction: a helper outside scripts/ importing a
    warehouse driver must fail the merge, not just be silently folded into
    shared_code_hash. Only L5 (dynamic imports) was enforced on closure
    members before this fix; L1 was not."""
    repo = tmp_path
    (repo / "scripts").mkdir()
    (repo / "lib").mkdir()
    (repo / "lib" / "shared.py").write_text("import psycopg2\n")
    (repo / "scripts" / "node.py").write_text(
        "from lib.shared import fetch_it\n\n\ndef run(ctx):\n    return ctx.read('ids')\n"
    )
    _write_single_node_contract(repo, "scripts/node.py")

    with pytest.raises(ContractError) as exc_info:
        build_wire_contract(repo / "contracts", repo, "s")

    message = str(exc_info.value)
    assert "analytics.t" in message
    assert "lib/shared.py:1: forbidden warehouse driver import 'psycopg2'" in message


def test_closure_member_sql_string_literal_raises(tmp_path):
    repo = tmp_path
    (repo / "scripts").mkdir()
    (repo / "lib").mkdir()
    (repo / "lib" / "shared.py").write_text(
        "def query():\n    return 'select secret from analytics.private'\n"
    )
    (repo / "scripts" / "node.py").write_text(
        "from lib.shared import query\n\n\ndef run(ctx):\n    return ctx.read('ids')\n"
    )
    _write_single_node_contract(repo, "scripts/node.py")

    with pytest.raises(ContractError, match=r"lib/shared\.py:2: SQL string literal"):
        build_wire_contract(repo / "contracts", repo, "s")


def test_closure_member_forbidden_data_access_call_raises(tmp_path):
    repo = tmp_path
    (repo / "scripts").mkdir()
    (repo / "lib").mkdir()
    (repo / "lib" / "shared.py").write_text(
        "def fetch_it(conn):\n    return conn.execute('noop')\n"
    )
    (repo / "scripts" / "node.py").write_text(
        "from lib.shared import fetch_it\n\n\ndef run(ctx):\n    return ctx.read('ids')\n"
    )
    _write_single_node_contract(repo, "scripts/node.py")

    with pytest.raises(
        ContractError, match=r"lib/shared\.py:2: forbidden data-access call 'execute'"
    ):
        build_wire_contract(repo / "contracts", repo, "s")


def test_violations_from_two_closure_members_both_appear_in_one_error(tmp_path):
    """A lint report that stops at the first offending file makes fixing an
    iterative guessing game - every file's violations must be reported
    together, in one raise."""
    repo = tmp_path
    (repo / "scripts").mkdir()
    (repo / "lib").mkdir()
    (repo / "lib" / "shared.py").write_text("import psycopg2\n")
    (repo / "lib" / "other.py").write_text(
        "def query():\n    return 'select secret from analytics.private'\n"
    )
    (repo / "scripts" / "node.py").write_text(
        "from lib.shared import fetch_it\n"
        "from lib.other import query\n\n\n"
        "def run(ctx):\n    return ctx.read('ids')\n"
    )
    _write_single_node_contract(repo, "scripts/node.py")

    with pytest.raises(ContractError) as exc_info:
        build_wire_contract(repo / "contracts", repo, "s")

    message = str(exc_info.value)
    assert "lib/shared.py:1: forbidden warehouse driver import 'psycopg2'" in message
    assert "lib/other.py:2: SQL string literal" in message


def test_script_itself_with_lint_violation_raises(tmp_path):
    """A node whose script was never covered by the CI lint step (workflow
    edited, different layout) must still fail at merge time - "everything
    that executes is linted" applies to the script too, not only to helpers
    it imports."""
    repo = tmp_path
    (repo / "scripts").mkdir()
    (repo / "scripts" / "node.py").write_text(
        "import psycopg2\n\n\ndef run(ctx):\n    return ctx.read('ids')\n"
    )
    _write_single_node_contract(repo, "scripts/node.py")

    with pytest.raises(
        ContractError, match=r"scripts/node\.py:1: forbidden warehouse driver import 'psycopg2'"
    ):
        build_wire_contract(repo / "contracts", repo, "s")


def test_clean_closure_merge_hashes_match_hand_computed(tmp_path):
    """Guard against the lint pass (review-fix-b) perturbing the artifact:
    for a closure with zero violations, source_hash and shared_code_hash
    must be byte-identical to hashes computed directly from the files on
    disk, independent of the lint call."""
    import hashlib

    repo = tmp_path
    (repo / "scripts").mkdir()
    (repo / "scripts" / "helpers.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "scripts" / "node.py").write_text(
        "import scripts.helpers\n\n\ndef run(ctx):\n    return ctx.read('ids')\n"
    )
    _write_single_node_contract(repo, "scripts/node.py")

    (entry,) = build_wire_contract(repo / "contracts", repo, "s")["nodes"]

    script_bytes = (repo / "scripts" / "node.py").read_bytes()
    helper_bytes = (repo / "scripts" / "helpers.py").read_bytes()
    expected_source_hash = hashlib.sha256(script_bytes).hexdigest()
    expected_shared_hash = hashlib.sha256(
        hashlib.sha256(helper_bytes).hexdigest().encode()
    ).hexdigest()

    assert entry["source_hash"] == expected_source_hash
    assert entry["shared_code_hash"] == expected_shared_hash
    assert set(entry) == WIRE_ENTRY_KEYS


def test_write_wire_contract_creates_missing_out_dir(contract_repo, tmp_path):
    """write_wire_contract should create --out's parent directory rather than
    crashing with FileNotFoundError."""
    doc = build_wire_contract(contract_repo / "contracts", contract_repo, "s")
    out = tmp_path / "does" / "not" / "exist" / "contract.yaml"
    write_wire_contract(doc, out)
    assert out.exists()
    assert yaml.safe_load(out.read_text())["service"] == "s"
