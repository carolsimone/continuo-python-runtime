"""The adapter entry-point tests carry one shared shim; its copies must not drift."""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENGINES = ("postgres", "trino", "duckdb")


def _shim(engine: str) -> str:
    path = ROOT / "adapters" / engine / "tests" / f"test_integration_{engine}_entrypoint.py"
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "_SHIM" for t in node.targets):
            value = ast.literal_eval(node.value)
            assert isinstance(value, str)
            return value
    pytest.fail(f"{path} has no _SHIM constant")


def test_the_three_entry_point_tests_use_the_same_shim():
    shims = {engine: _shim(engine) for engine in ENGINES}
    assert len(set(shims.values())) == 1, shims
