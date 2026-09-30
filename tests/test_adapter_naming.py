"""Guard: for every installed engine adapter, the distribution name, the import
module, and the entry-point target agree — so `pip install continuo-<engine>-adapter`
always yields `import continuo_<engine>_adapter`. Regressing this reintroduces the
pip-name != import-name trap and breaks the deterministic BYO template.
"""
import importlib
import importlib.metadata as md

import pytest

from continuo_engine_contract.port import ENTRY_POINT_GROUP


def _entry_points():
    return list(md.entry_points(group=ENTRY_POINT_GROUP))


def test_the_known_engines_are_installed():
    names = {ep.name for ep in _entry_points()}
    assert {"postgres", "trino", "duckdb"} <= names, f"missing engines; found {sorted(names)}"


@pytest.mark.parametrize("ep", _entry_points(), ids=lambda ep: ep.name)
def test_adapter_names_follow_the_convention(ep):
    engine = ep.name
    expected_dist = f"continuo-{engine}-adapter"
    expected_module = f"continuo_{engine}_adapter"
    # entry-point target lives in the expected top-level import package (the
    # target itself is a submodule, e.g. "continuo_postgres_adapter.adapter")
    top_level = ep.module.split(".")[0]
    assert top_level == expected_module, (
        f"entry point {engine!r} targets {ep.value!r}, expected top-level module "
        f"{expected_module!r}"
    )
    # the target module imports
    importlib.import_module(expected_module)
    # the distribution providing it carries the expected pip name
    assert ep.dist is not None and ep.dist.name == expected_dist, (
        f"entry point {engine!r} is provided by {getattr(ep.dist, 'name', None)!r}, "
        f"expected {expected_dist!r}"
    )
