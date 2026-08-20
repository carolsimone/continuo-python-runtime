"""Unit tests for the SQL column-type grammar contract."""
import pytest

from continuo_engine_contract.types import validate_column_type


@pytest.mark.parametrize("ok", [
    "BIGINT", "INT", "INTEGER", "DOUBLE PRECISION", "TEXT",
    "TIMESTAMP", "DATE", "BOOLEAN",
    "NUMERIC(10,2)", "DECIMAL(38, 0)", "VARCHAR(255)", "CHAR(1)",
    "bigint", "varchar(9)",  # case-insensitive
])
def test_supported_types_pass(ok: str) -> None:
    """Supported types must not raise."""
    validate_column_type(ok)  # must not raise


@pytest.mark.parametrize("bad", [
    "", "FLOAT", "VARCHAR", "NUMERIC", "NUMERIC(10)", "VARCHAR(255); DROP TABLE x",
    "TEXT )", "INTEGER--", "SERIAL", "JSONB", "TIMESTAMP WITH TIME ZONE",
])
def test_unsupported_or_malformed_types_raise(bad: str) -> None:
    """Unsupported or malformed types must raise ValueError."""
    with pytest.raises(ValueError):
        validate_column_type(bad)
