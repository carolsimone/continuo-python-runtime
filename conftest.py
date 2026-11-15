"""Fixtures for every suite of the repository: the root tests and the adapters' tests.

Each adapter's tests directory is its own `tests` package, so the root helper
`tests/odcs_builder.py` cannot be imported by name from there, and a relative
import between an adapter's modules resolves only for the first adapter pytest
collects in a session. The adapters' integration tests reach the helper through
this fixture, which loads it from its file.
"""
import importlib.util
from collections.abc import Callable
from pathlib import Path

import pytest

_ODCS_BUILDER = Path(__file__).resolve().parent / "tests" / "odcs_builder.py"


@pytest.fixture(scope="session")
def write_contract() -> Callable[..., Path]:
    """`tests/odcs_builder.write_contract`: write `<table>.odcs.yaml` for the node its fields describe.

    `write_contract(contracts_dir, **fields)` takes the fields of the runtime's
    `Node` (`schema`, `table`, `owner`, `reads`, `output_columns`, `mode_type`,
    `primary_key`, ...), creates the folder when it does not exist and returns
    the file's path.
    """
    spec = importlib.util.spec_from_file_location("odcs_builder", _ODCS_BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    writer: Callable[..., Path] = module.write_contract
    return writer
