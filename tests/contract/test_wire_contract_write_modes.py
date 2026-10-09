"""The shared write-modes wire fixture, reproduced by the real loader and node_entry().

The fixture has a byte-identical twin in continuo
(topology-controller/tests/fixtures/wire_contract_write_modes.yml); its header
names both copies. Each fixture entry doubles as the author's contract node:
parsing it and emitting it again must give back exactly the same entry.
"""
from pathlib import Path

import pytest
import yaml

from continuo_python_runtime.contract.loader import parse_node
from continuo_python_runtime.contract.merge import node_entry

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "wire_contract_write_modes.yml"
HASH_FIELDS = {"source_hash", "shared_code_hash", "config_hash", "content_hash"}


def _fixture_nodes() -> list[dict]:
    return yaml.safe_load(FIXTURE.read_text())["nodes"]


@pytest.mark.parametrize("expected", _fixture_nodes(), ids=lambda entry: entry["table"])
def test_node_entry_reproduces_the_fixture_entry(expected):
    node = parse_node(dict(expected), FIXTURE.name)
    assert node_entry(node) == expected


def test_fixture_entries_carry_no_hash_fields():
    for entry in _fixture_nodes():
        assert not HASH_FIELDS & set(entry), entry["table"]


def test_replace_entry_has_no_mode_keys_even_when_replace_is_authored():
    (replace,) = [e for e in _fixture_nodes() if e["table"] == "replace_node"]
    assert "mode_type" not in replace and "primary_key" not in replace
    node = parse_node({**replace, "mode_type": "replace"}, FIXTURE.name)
    assert node_entry(node) == replace


def test_fixture_covers_every_mode_and_a_key():
    entries = _fixture_nodes()
    assert {e.get("mode_type", "replace") for e in entries} == {"append", "upsert", "replace"}
    assert [e["primary_key"] for e in entries if "primary_key" in e] == [["rate_date", "base"]]


def test_fixture_names_both_twins():
    text = FIXTURE.read_text()
    # Both twin paths are named in the header. Check each via a form that is not
    # a substring of the other, so neither assertion is vacuous.
    assert "topology-controller/tests/fixtures/wire_contract_write_modes.yml" in text
    assert "continuo-python-runtime: tests/fixtures/wire_contract_write_modes.yml" in text
