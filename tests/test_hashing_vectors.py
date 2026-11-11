"""The library's hashing vectors, reproduced through the runtime's own hash functions.

The runtime folds a Python node's three parts (`fold3`) and its import closure (`shared_hash`).
The vector file also holds `fold4` and `shared_fold`, which the runtime has no function for, so
those cases are left out by selection.
"""
import json
from importlib import resources

import pytest

from continuo_python_runtime.hashing import content_hash_fold, shared_code_hash

VECTORS = json.loads(
    resources.files("continuo_contracts").joinpath("vectors/hashing.json").read_text(encoding="utf-8")
)
RUNTIME_FUNCTIONS = ("fold3", "shared_hash")
RUNNABLE = [case for case in VECTORS if case["function"] in RUNTIME_FUNCTIONS]


def _run(case: dict) -> str:
    inputs = case["inputs"]
    if case["function"] == "fold3":
        return content_hash_fold(**inputs)
    # `units_hex` holds each unit's bytes as lowercase hex.
    return shared_code_hash({path: bytes.fromhex(data) for path, data in inputs["units_hex"].items()})


def test_the_library_ships_a_case_for_each_function_the_runtime_has():
    assert {case["function"] for case in RUNNABLE} == set(RUNTIME_FUNCTIONS)


@pytest.mark.parametrize("case", RUNNABLE, ids=lambda case: case["name"])
def test_the_runtime_reproduces_each_hashing_vector(case):
    assert _run(case) == case["expected"]
