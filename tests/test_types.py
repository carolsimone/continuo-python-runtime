import pyarrow as pa
import pytest

from continuo_python_runtime.errors import ContractError
from continuo_python_runtime.types import SqlType, arrow_type, parse_sql_type


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("BIGINT", SqlType("BIGINT")),
        ("int", SqlType("INTEGER")),
        ("Integer", SqlType("INTEGER")),
        ("DOUBLE PRECISION", SqlType("DOUBLE_PRECISION")),
        ("NUMERIC(10,2)", SqlType("NUMERIC", precision=10, scale=2)),
        ("decimal(5, 0)", SqlType("NUMERIC", precision=5, scale=0)),
        ("VARCHAR(255)", SqlType("VARCHAR", length=255)),
        ("char(3)", SqlType("CHAR", length=3)),
        ("TEXT", SqlType("TEXT")),
        ("TIMESTAMP", SqlType("TIMESTAMP")),
        ("DATE", SqlType("DATE")),
        ("BOOLEAN", SqlType("BOOLEAN")),
    ],
)
def test_parse_supported(raw, expected):
    assert parse_sql_type(raw) == expected


@pytest.mark.parametrize("raw", ["JSONB", "VARCHAR", "NUMERIC", "NUMERIC(10)", "FLOAT", ""])
def test_parse_unsupported_raises(raw):
    with pytest.raises(ContractError):
        parse_sql_type(raw)


def test_arrow_mapping():
    assert arrow_type(parse_sql_type("NUMERIC(10,2)")) == pa.decimal128(10, 2)
    assert arrow_type(parse_sql_type("VARCHAR(9)")) == pa.string()
    assert arrow_type(parse_sql_type("TIMESTAMP")) == pa.timestamp("us")
    assert arrow_type(parse_sql_type("INT")) == pa.int32()
