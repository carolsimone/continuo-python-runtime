import pytest

from continuo_python_runtime.errors import (
    ConformError,
    ContractError,
    HarnessError,
    LoadError,
    ReadError,
    ScriptError,
)


@pytest.mark.parametrize(
    "exc_type,name",
    [
        (ContractError, "ContractError"),
        (ReadError, "ReadError"),
        (ScriptError, "ScriptError"),
        (ConformError, "ConformError"),
        (LoadError, "LoadError"),
    ],
)
def test_error_class_and_sentinel_message(exc_type, name):
    err = exc_type("boom")
    assert isinstance(err, HarnessError)
    assert err.error_class == name
    assert err.sentinel_message() == f"{name}: boom"
