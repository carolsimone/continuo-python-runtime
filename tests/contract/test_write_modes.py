"""The write-mode fixtures: one ODCS contract per write mode, read by the real loader.

`tests/fixtures/write_modes` holds four contracts, one per way a node writes its
table (replace, append, upsert) and one for a node that ships an asset. Each
declares the node that `DECLARED` describes, with the fields of the library's
`PythonNode` plus the owner, schedule, criticality and description: the loader
reads that node from the file, and the node projects and hashes as the
declaration says.
"""
from pathlib import Path

import pytest
from continuo_contracts.python import PythonNode, code_projection, projection_hash

from continuo_python_runtime.contract.loader import load_contract_dir
from continuo_python_runtime.contract.model import Node
from continuo_python_runtime.hash_inputs import gather_hash_inputs
from continuo_python_runtime.hashing import hash_parts
from tests.odcs_builder import contract_node
from tests.test_hashing import old_hash_parts

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "write_modes"

_COMMON = {
    "schema": "fixture", "owner": "data-team", "schedule": "hourly", "criticality": "SECONDARY",
    "description": "", "extra_columns": "raise", "config": {},
}
DECLARED = {
    "replace_node": {
        **_COMMON, "table": "replace_node", "kind": "python-node", "script": "scripts/replace_node.py",
        "reads": {"src": "SELECT id FROM fixture.src"},
        "output_columns": [{"name": "id", "type": "BIGINT", "nullable": False}],
    },
    "append_node": {
        **_COMMON, "table": "append_node", "kind": "python-api", "script": "scripts/append_node.py",
        "reads": {},
        "output_columns": [
            {"name": "collected_at", "type": "TIMESTAMP", "nullable": False},
            {"name": "rate", "type": "DECIMAL(12,6)", "nullable": True},
        ],
        "mode_type": "append",
    },
    "upsert_node": {
        **_COMMON, "table": "upsert_node", "kind": "python-api", "script": "scripts/upsert_node.py",
        "reads": {},
        "output_columns": [
            {"name": "rate_date", "type": "DATE", "nullable": False},
            {"name": "base", "type": "VARCHAR(3)", "nullable": False},
            {"name": "rate", "type": "DECIMAL(12,6)", "nullable": True},
        ],
        "mode_type": "upsert", "primary_key": ["rate_date", "base"],
    },
    "assets_node": {
        **_COMMON, "table": "assets_node", "kind": "python-node", "script": "scripts/assets_node.py",
        "reads": {"src": "SELECT id FROM fixture.src"},
        "output_columns": [{"name": "id", "type": "BIGINT", "nullable": False}],
        "assets": ["data/ref.json"],
    },
}


@pytest.fixture(scope="module")
def nodes() -> dict[str, Node]:
    """The nodes the fixture folder declares, by table, read with every loader check."""
    return {node.table: node for node in load_contract_dir(FIXTURES)}


@pytest.fixture
def repo(tmp_path) -> Path:
    """A repository holding the scripts and the asset the fixtures name."""
    for entry in DECLARED.values():
        script = tmp_path / entry["script"]
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(f"def run(ctx):\n    return '{entry['table']}'\n")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "ref.json").write_text('{"currencies": ["EUR", "GBP"]}\n')
    return tmp_path


def test_the_fixture_folder_holds_one_contract_per_declared_node(nodes):
    assert sorted(nodes) == sorted(DECLARED)
    assert sorted(path.name for path in FIXTURES.iterdir()) == sorted(f"{table}.odcs.yaml" for table in DECLARED)


@pytest.mark.parametrize("table", DECLARED)
def test_each_fixture_declares_the_node_it_describes(table, nodes):
    declared, node = DECLARED[table], nodes[table]
    assert node.relation == f"{declared['schema']}.{declared['table']}"
    assert (node.owner, node.schedule, node.criticality, node.description) == (
        declared["owner"], declared["schedule"], declared["criticality"], declared["description"],
    )
    assert (node.kind, node.script, node.reads, node.extra_columns, node.config) == (
        declared["kind"], declared["script"], declared["reads"], declared["extra_columns"], declared["config"],
    )
    assert [
        {"name": c.name, "type": c.type, "nullable": c.nullable} for c in node.output_columns
    ] == declared["output_columns"]
    assert (node.mode_type, node.primary_key, node.assets) == (
        declared.get("mode_type", "replace"),
        tuple(declared.get("primary_key", ())),
        tuple(declared.get("assets", ())),
    )


@pytest.mark.parametrize("table", DECLARED)
def test_each_fixture_projects_as_the_node_it_describes(table, nodes):
    expected = PythonNode.from_mapping(DECLARED[table])
    assert code_projection(nodes[table].python) == code_projection(expected)
    assert projection_hash(nodes[table].python) == projection_hash(expected)


@pytest.mark.parametrize("table", DECLARED)
def test_each_fixture_hashes_as_the_node_it_describes(table, nodes, repo):
    inputs = gather_hash_inputs(nodes[table], repo)
    assert hash_parts(nodes[table], inputs.primary, inputs.units) == old_hash_parts(
        DECLARED[table], repo, inputs.units
    )


def test_the_assets_node_hashes_the_file_it_names(nodes, repo):
    assert gather_hash_inputs(nodes["assets_node"], repo).units == {
        "data/ref.json": (repo / "data" / "ref.json").read_bytes()
    }


def test_a_replace_node_projects_the_same_whether_or_not_it_names_its_mode(nodes):
    authored = contract_node(
        schema="fixture", table="replace_node", owner="data-team", schedule="hourly",
        criticality="SECONDARY", kind="python-node", script="scripts/replace_node.py",
        reads={"src": "SELECT id FROM fixture.src"},
        output_columns=[{"name": "id", "type": "BIGINT", "nullable": False}],
        mode_type="replace",
    )
    assert authored.mode_type == "replace" and authored.primary_key == ()
    assert code_projection(authored.python) == code_projection(nodes["replace_node"].python)
    assert projection_hash(authored.python) == projection_hash(nodes["replace_node"].python)


def test_the_fixtures_cover_every_mode_and_a_key(nodes):
    assert {node.mode_type for node in nodes.values()} == {"append", "upsert", "replace"}
    assert [node.primary_key for node in nodes.values() if node.primary_key] == [("rate_date", "base")]


def test_the_fixtures_cover_assets(nodes):
    assert [node.assets for node in nodes.values() if node.assets] == [("data/ref.json",)]
