"""The library's projection vectors, reproduced by the runtime's loader from the contract of each vector's node."""
import json
from importlib import resources

import pytest
from continuo_contracts.python import code_projection

from continuo_python_runtime.hashing import config_hash, to_python_node
from tests.odcs_builder import contract_node

VECTORS = json.loads(resources.files("continuo_contracts").joinpath("vectors/projection.json").read_text())
CONTRACT_ONLY = {"owner": "vectors", "schedule": "daily", "criticality": "SECONDARY"}


def _authored(node: dict) -> dict:
    """The vector's node as contract fields, without the defaults some kinds refuse as properties."""
    authored = {**node, **CONTRACT_ONLY}
    for key, default in (("secret_ref", ""), ("assets", []), ("script", ""), ("reads", {})):
        if authored.get(key) == default:
            authored.pop(key)
    if authored.get("mode_type") != "upsert":
        authored.pop("primary_key", None)
    return authored


def test_the_library_ships_a_vector_for_every_kind():
    assert {v["node"].get("kind", "python-node") for v in VECTORS} == {"python-node", "python-csv", "python-api"}


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: vector["name"])
def test_the_runtime_reproduces_each_projection_vector(vector):
    node = contract_node(**_authored(vector["node"]))
    assert json.loads(json.dumps(code_projection(to_python_node(node)))) == vector["projection"]
    assert config_hash(node) == vector["projection_hash"]
