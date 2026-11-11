"""The library's projection vectors, reproduced from authored entries by the runtime's own loader."""
import json
from importlib import resources

import pytest
from continuo_contracts.python import code_projection

from continuo_python_runtime.contract.loader import parse_node
from continuo_python_runtime.hashing import config_hash, to_python_node

VECTORS = json.loads(resources.files("continuo_contracts").joinpath("vectors/projection.json").read_text())
CONTRACT_ONLY = {"owner": "vectors", "schedule": "daily", "criticality": "SECONDARY"}


def _authored(node: dict) -> dict:
    """The vector's node as an authored entry, without the defaults some kinds refuse as keys."""
    authored = {**node, **CONTRACT_ONLY}
    for key, default in (("secret_ref", ""), ("assets", []), ("script", "")):
        if authored.get(key) == default:
            authored.pop(key)
    if authored.get("mode_type") != "upsert":
        authored.pop("primary_key", None)
    return authored


def test_the_library_ships_a_vector_for_every_kind():
    assert {v["node"].get("kind", "python-node") for v in VECTORS} == {"python-node", "python-csv", "python-api"}


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: vector["name"])
def test_the_runtime_reproduces_each_projection_vector(vector):
    node = parse_node(_authored(vector["node"]), vector["name"], check_reads=False)
    assert json.loads(json.dumps(code_projection(to_python_node(node)))) == vector["projection"]
    assert config_hash(node) == vector["projection_hash"]
