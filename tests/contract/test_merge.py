"""Tests for contract v1 merger."""

from hashlib import sha256

import yaml

import pytest

from continuo_python_runtime.contract.loader import parse_node
from continuo_python_runtime.contract.merge import (
    build_wire_contract,
    node_entry,
    write_wire_contract,
)
from continuo_python_runtime.errors import ContractError
from continuo_python_runtime.hashing import config_hash, content_hash_fold

WIRE_ENTRY_KEYS = {
    "schema", "table", "owner", "schedule", "criticality", "script", "reads",
    "output_columns", "description", "extra_columns", "config", "kind",
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


def test_closure_member_with_latin1_coding_cookie_raises_contract_error(tmp_path):
    """review-fix-d P1 reproduction: a closure member declaring a PEP 263
    coding cookie (`# -*- coding: latin-1 -*-`) plus a non-ASCII byte parses
    fine under `ast.parse(bytes)` in `resolve_closure` (which honors the
    cookie), but the merger's own lint pass unconditionally decodes every
    file as UTF-8. That decode must raise a UnicodeDecodeError for this file
    (0xe9 is not valid UTF-8 on its own) - the bug is that this escaped as a
    bare stdlib exception instead of a ContractError naming the node and
    file. Must never surface as anything but ContractError."""
    repo = tmp_path
    (repo / "scripts").mkdir()
    (repo / "lib").mkdir()
    (repo / "lib" / "shared.py").write_bytes(
        "# -*- coding: latin-1 -*-\n# caf\xe9\nX = 1\n".encode("latin-1")
    )
    (repo / "scripts" / "node.py").write_text(
        "from lib.shared import X\n\n\ndef run(ctx):\n    return ctx.read('ids')\n"
    )
    _write_single_node_contract(repo, "scripts/node.py")

    with pytest.raises(ContractError) as exc_info:
        build_wire_contract(repo / "contracts", repo, "s")

    message = str(exc_info.value)
    assert "analytics.t" in message
    assert "lib/shared.py" in message


def test_closure_member_with_utf8_bom_does_not_raise_bare_syntax_error(tmp_path):
    """review-fix-d P1 reproduction: a UTF-8 BOM file is imported by CPython
    without complaint and `ast.parse(bytes)` in `resolve_closure` accepts it
    too (bytes-mode parsing strips a leading BOM as an implicit encoding
    declaration). But decoding those same bytes with plain 'utf-8' keeps the
    BOM as a literal U+FEFF character in the resulting str, and re-parsing
    *that string* raises 'invalid non-printable character U+FEFF', blocking
    the merge for a file that runs fine. Decoding as 'utf-8-sig' strips the
    BOM exactly like bytes-mode parsing does, so the merge must succeed."""
    repo = tmp_path
    (repo / "scripts").mkdir()
    (repo / "lib").mkdir()
    (repo / "lib" / "shared.py").write_bytes(b"\xef\xbb\xbfX = 1\n")
    (repo / "scripts" / "node.py").write_text(
        "from lib.shared import X\n\n\ndef run(ctx):\n    return ctx.read('ids')\n"
    )
    _write_single_node_contract(repo, "scripts/node.py")

    doc = build_wire_contract(repo / "contracts", repo, "s")

    (entry,) = doc["nodes"]
    assert entry["shared_code_hash"] != ""


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


def test_wire_entry_carries_kind(contract_repo):
    repo = contract_repo
    doc = build_wire_contract(repo / "contracts", repo, "s")
    assert doc["nodes"][0]["kind"] == "python-node"


def test_csv_wire_entry_hashes_the_uri(tmp_path):
    uri = "s3://drops/orders.csv"
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "c.yml").write_text(
        "nodes:\n"
        "  - schema: analytics\n"
        "    table: orders_csv\n"
        "    owner: team\n"
        "    schedule: daily\n"
        "    criticality: SECONDARY\n"
        "    kind: python-csv\n"
        f"    reads: {{csv: {uri}}}\n"
        "    output_columns:\n"
        "      - {name: order_id, type: INTEGER, nullable: false}\n"
    )
    doc = build_wire_contract(tmp_path / "contracts", tmp_path, "svc")
    entry = doc["nodes"][0]
    assert entry["kind"] == "python-csv"
    assert entry["script"] == ""
    assert entry["source_hash"] == sha256(uri.encode()).hexdigest()
    assert entry["shared_code_hash"] == ""
    assert entry["content_hash"] == content_hash_fold(
        entry["source_hash"], "", entry["config_hash"])


def test_csv_node_needs_no_script_file_on_disk(tmp_path):
    # same fixture as above: note NO scripts/ dir exists — must not raise
    uri = "s3://drops/orders.csv"
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "c.yml").write_text(
        "nodes:\n"
        "  - schema: analytics\n"
        "    table: orders_csv\n"
        "    owner: team\n"
        "    schedule: daily\n"
        "    criticality: SECONDARY\n"
        "    kind: python-csv\n"
        f"    reads: {{csv: {uri}}}\n"
        "    output_columns:\n"
        "      - {name: order_id, type: INTEGER, nullable: false}\n"
    )
    doc = build_wire_contract(tmp_path / "contracts", tmp_path, "svc")
    assert len(doc["nodes"]) == 1


def _api_repo(tmp_path, **extra):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "contracts").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "fx.py").write_text(
        "import pyarrow as pa\n\ndef run(ctx):\n    return pa.table({'rate': [1.0]})\n"
    )
    node = {
        "schema": "analytics", "table": "fx", "owner": "m", "schedule": "daily",
        "criticality": "SECONDARY", "kind": "python-api", "script": "scripts/fx.py",
        "output_columns": [{"name": "rate", "type": "DOUBLE PRECISION"}],
    }
    node.update(extra)
    (tmp_path / "contracts" / "fx.yml").write_text(yaml.safe_dump({"nodes": [node]}))
    return tmp_path


def test_python_api_wire_entry_has_empty_reads_and_no_secret_key_when_unset(tmp_path):
    repo = _api_repo(tmp_path)
    (entry,) = build_wire_contract(repo / "contracts", repo, "s")["nodes"]
    assert set(entry) == WIRE_ENTRY_KEYS
    assert entry["kind"] == "python-api"
    assert entry["reads"] == {}
    assert entry["source_hash"] == sha256((repo / "scripts" / "fx.py").read_bytes()).hexdigest()


def test_python_api_wire_entry_carries_secret_ref_when_set(tmp_path):
    repo = _api_repo(tmp_path, secret_ref="continuo-api-fx")
    (entry,) = build_wire_contract(repo / "contracts", repo, "s")["nodes"]
    assert set(entry) == WIRE_ENTRY_KEYS | {"secret_ref"}
    assert entry["secret_ref"] == "continuo-api-fx"


def test_secret_ref_is_part_of_the_config_hash(tmp_path):
    a = _api_repo(tmp_path / "a", secret_ref="continuo-api-fx")
    b = _api_repo(tmp_path / "b", secret_ref="continuo-api-other")
    (ea,) = build_wire_contract(a / "contracts", a, "s")["nodes"]
    (eb,) = build_wire_contract(b / "contracts", b, "s")["nodes"]
    assert ea["source_hash"] == eb["source_hash"]
    assert ea["config_hash"] != eb["config_hash"]


def test_python_api_script_is_held_to_the_closure_lint(tmp_path):
    repo = _api_repo(tmp_path)
    (repo / "scripts" / "fx.py").write_text("import psycopg2\n\ndef run(ctx):\n    return None\n")
    with pytest.raises(ContractError, match="lint violations"):
        build_wire_contract(repo / "contracts", repo, "s")


# --- write modes on the wire --------------------------------------------------


def _modes_repo(tmp_path, **extra):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "contracts").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "fx.py").write_text(
        "import pyarrow as pa\n\ndef run(ctx):\n    return pa.table({'rate': [1.0]})\n"
    )
    node = {
        "schema": "fx", "table": "fx_rates", "owner": "m", "schedule": "hourly",
        "criticality": "SECONDARY", "kind": "python-api", "script": "scripts/fx.py",
        "output_columns": [
            {"name": "rate_date", "type": "DATE", "nullable": False},
            {"name": "base", "type": "VARCHAR(3)", "nullable": False},
            {"name": "rate", "type": "DECIMAL(12,6)"},
        ],
    }
    node.update(extra)
    (tmp_path / "contracts" / "fx.yml").write_text(yaml.safe_dump({"nodes": [node]}))
    return tmp_path


def _only_entry(repo):
    (entry,) = build_wire_contract(repo / "contracts", repo, "s")["nodes"]
    return entry


def test_replace_entry_is_identical_whether_or_not_mode_type_is_authored(tmp_path):
    implicit = _only_entry(_modes_repo(tmp_path / "a"))
    explicit = _only_entry(_modes_repo(tmp_path / "b", mode_type="replace"))
    assert set(explicit) == WIRE_ENTRY_KEYS
    assert explicit == implicit


def test_append_entry_carries_mode_type_and_no_key(tmp_path):
    entry = _only_entry(_modes_repo(tmp_path, mode_type="append"))
    assert set(entry) == WIRE_ENTRY_KEYS | {"mode_type"}
    assert entry["mode_type"] == "append"


def test_upsert_entry_carries_its_key_in_authored_order(tmp_path):
    entry = _only_entry(
        _modes_repo(tmp_path, mode_type="upsert", primary_key=["base", "rate_date"])
    )
    assert set(entry) == WIRE_ENTRY_KEYS | {"mode_type", "primary_key"}
    assert entry["mode_type"] == "upsert"
    assert entry["primary_key"] == ["base", "rate_date"]


def test_mode_type_and_primary_key_are_part_of_the_config_hash(tmp_path):
    append = _only_entry(_modes_repo(tmp_path / "a", mode_type="append"))
    upsert = _only_entry(
        _modes_repo(tmp_path / "b", mode_type="upsert", primary_key=["rate_date", "base"])
    )
    reordered = _only_entry(
        _modes_repo(tmp_path / "c", mode_type="upsert", primary_key=["base", "rate_date"])
    )
    assert append["source_hash"] == upsert["source_hash"] == reordered["source_hash"]
    assert len({append["config_hash"], upsert["config_hash"], reordered["config_hash"]}) == 3


# Computed with continuo-python-runtime 0.8.0 (before mode_type existed) for the
# shared fixture's replace node. A replace entry must hash exactly like this.
_PRE_WRITE_MODES_CONFIG_HASH = "651db2a6fdafda3f4dcdd5358132b0d7cd0177836be90380cd79388ac31d8cdf"
_REPLACE_NODE = {
    "schema": "fixture", "table": "replace_node", "owner": "data-team",
    "schedule": "hourly", "criticality": "SECONDARY", "kind": "python-node",
    "script": "scripts/replace_node.py", "reads": {"src": "SELECT id FROM fixture.src"},
    "output_columns": [{"name": "id", "type": "BIGINT", "nullable": False}],
    "description": "", "extra_columns": "raise", "config": {},
}


@pytest.mark.parametrize("extra", [{}, {"mode_type": "replace"}])
def test_a_replace_entry_hashes_exactly_as_it_did_before_write_modes(extra):
    entry = node_entry(parse_node({**_REPLACE_NODE, **extra}, "f.yml"))
    assert "mode_type" not in entry and "primary_key" not in entry
    assert config_hash(entry) == _PRE_WRITE_MODES_CONFIG_HASH


# --- timeout_minutes ----------------------------------------------------------


def test_timeout_minutes_is_outside_the_config_hash(tmp_path):
    without = _only_entry(_modes_repo(tmp_path / "a"))
    short = _only_entry(_modes_repo(tmp_path / "b", timeout_minutes=120))
    long = _only_entry(_modes_repo(tmp_path / "c", timeout_minutes=480))
    assert short["config_hash"] == long["config_hash"] == without["config_hash"]
    assert short["content_hash"] == long["content_hash"] == without["content_hash"]
    # The wire entry hashes exactly as the same entry would without the key.
    assert config_hash(short) == short["config_hash"]


def test_the_wire_entry_carries_timeout_minutes_only_when_set(tmp_path):
    without = _only_entry(_modes_repo(tmp_path / "a"))
    declared = _only_entry(_modes_repo(tmp_path / "b", timeout_minutes=120))
    assert "timeout_minutes" not in without
    assert set(without) == WIRE_ENTRY_KEYS
    assert declared["timeout_minutes"] == 120
    assert set(declared) == WIRE_ENTRY_KEYS | {"timeout_minutes"}
