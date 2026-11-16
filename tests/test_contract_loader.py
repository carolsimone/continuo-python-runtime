"""node_from_contract on one document: the runtime's rules on top of the library's.

The library checks the contract and maps it to its PythonNode; these tests pin
how the runtime reads that node, and the rules the runtime keeps for itself,
on a document handed over as a mapping.
"""
from pathlib import Path

import pytest
from continuo_contracts import yaml_strict

from continuo_python_runtime.contract.loader import node_from_contract
from continuo_python_runtime.errors import ContractError
from tests.odcs_builder import contract_text

FIELDS = {
    "schema": "analytics", "table": "t", "owner": "m", "schedule": "daily",
    "criticality": "SECONDARY", "script": "scripts/t.py",
    "reads": {"ids": "select id from analytics.a"},
    "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
}
FILE = Path("t.odcs.yaml")


def _doc(**over):
    return yaml_strict.load(contract_text(**{**FIELDS, **over}))


def _read(doc):
    return node_from_contract(doc, FILE, dialect=None, check_reads=True)


def test_python_model_runs_as_python_node():
    node = _read(_doc(kind="python-model"))
    assert node.kind == "python-node"
    assert node.python.kind == "python-node"


def test_a_descriptive_key_is_not_a_runtime_key():
    """On a replace node a key only describes the table: the harness upserts on nothing."""
    node = _read(_doc(primary_key=["id"]))
    assert node.mode_type == "replace"
    assert node.primary_key == ()
    assert tuple(node.python.primary_key) == ("id",)


def test_an_upsert_key_may_not_name_two_columns_that_differ_only_in_case():
    doc = _doc(
        mode_type="upsert",
        primary_key=["base", "Base"],
        output_columns=[
            {"name": "base", "type": "VARCHAR(3)", "nullable": False},
            {"name": "Base", "type": "VARCHAR(3)", "nullable": False},
        ],
    )
    with pytest.raises(
        ContractError,
        match=r"t\.odcs\.yaml \(analytics\.t\): the upsert key names 'Base' more than once "
        r"\(names are compared case-insensitively\)",
    ):
        _read(doc)


def test_an_upsert_key_may_name_one_of_two_columns_that_differ_only_in_case():
    doc = _doc(
        mode_type="upsert",
        primary_key=["Base"],
        output_columns=[
            {"name": "base", "type": "VARCHAR(3)", "nullable": False},
            {"name": "Base", "type": "VARCHAR(3)", "nullable": False},
        ],
    )
    assert _read(doc).primary_key == ("Base",)


def _cyclic():
    config: dict = {}
    config["indexes"] = [config]
    return config


@pytest.mark.parametrize(
    ("config", "match"),
    [
        ({"indexes": [{1: "x"}]}, "'config' keys must be strings, got 1"),
        ({"foo": {"bar": object()}}, "'config' value for 'bar' is not JSON-serializable"),
        (_cyclic(), "'config' contains a circular reference at 'indexes'"),
    ],
    ids=["non-string-key", "not-json", "cycle"],
)
def test_the_runtime_still_refuses_a_nested_config_value(config, match):
    """A document built in code can hold what no contract file can; the runtime refuses it."""
    doc = _doc(config={"placeholder": True})
    (prop,) = [p for p in doc["customProperties"] if p["property"] == "config"]
    prop["value"] = config
    with pytest.raises(ContractError, match=rf"t\.odcs\.yaml \(analytics\.t\): {match}"):
        _read(doc)


def test_library_issues_come_before_the_runtime_rules():
    doc = _doc(criticality="HIGH", config={"placeholder": True})
    (prop,) = [p for p in doc["customProperties"] if p["property"] == "config"]
    prop["value"] = {"k": object()}
    with pytest.raises(ContractError, match=r"^t\.odcs\.yaml: customProperties\[\d\]: `criticality`"):
        _read(doc)
