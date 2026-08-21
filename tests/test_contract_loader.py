"""tests/test_contract_loader.py — unit tests for parse_node's own validation
rules that are cheapest to exercise directly against a raw dict, rather than
through a full contract-directory fixture (see test_harness.py for those)."""
import pytest

from continuo_python_runtime.contract.loader import parse_node
from continuo_python_runtime.errors import ContractError


def _base_node(**over):
    node = {
        "schema": "analytics", "table": "t", "owner": "m", "schedule": "daily",
        "criticality": "SECONDARY", "script": "scripts/t.py",
        "reads": {"ids": "select id from analytics.a"},
        "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
    }
    node.update(over)
    return node


@pytest.mark.parametrize("bad_kind", [["python-csv"], {"k": "python-csv"}, 3, True, None])
def test_non_string_kind_raises_contracterror_not_typeerror(bad_kind):
    """A malformed `kind: [python-csv]` must fail membership-testing against
    the frozenset KINDS with a ContractError, not an unhandled bare TypeError
    from `kind not in KINDS` on an unhashable value (list/dict)."""
    with pytest.raises(ContractError, match="'kind' must be one of"):
        parse_node(_base_node(kind=bad_kind), "t.yml")


def test_valid_string_kind_still_accepted():
    node = parse_node(_base_node(kind="python-model"), "t.yml")
    assert node.kind == "python-model"
