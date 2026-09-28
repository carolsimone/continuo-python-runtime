"""Every declared kind has contract rules, hash inputs and a run producer.

A kind added to KINDS without one of these would fail at merge or at run
time; this pins the three registries to the one list.
"""

from continuo_python_runtime import harness
from continuo_python_runtime.contract import merge
from continuo_python_runtime.contract.kinds import RULES
from continuo_python_runtime.contract.model import KINDS


def test_every_kind_has_rules():
    assert set(RULES) == KINDS


def test_every_kind_has_hash_inputs():
    assert set(merge._HASH_INPUTS) == KINDS


def test_every_kind_has_a_producer():
    assert set(harness._PRODUCERS) == KINDS
