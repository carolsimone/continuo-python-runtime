import pyarrow as pa
import pytest
from continuo_validation_contract.types import validate_column_type  # type: ignore[import-untyped]

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


@pytest.mark.parametrize(
    "raw",
    [
        "JSONB",
        "VARCHAR",
        "NUMERIC",
        "NUMERIC(10)",
        "FLOAT",
        "",
        "VARCHAR(-1)",
        "CHAR(-1)",
        "NUMERIC(-5,2)",
        "NUMERIC(10, -2)",
        "NUMERIC(10 ,2)",
        "NUMERIC(0,0)",
        "NUMERIC(39,0)",
        "NUMERIC(5,6)",
        "DOUBLE_PRECISION",
    ],
)
def test_parse_unsupported_raises(raw):
    with pytest.raises(ContractError):
        parse_sql_type(raw)


def test_numeric_precision_out_of_range_raises():
    with pytest.raises(ContractError, match="precision"):
        parse_sql_type("NUMERIC(0,0)")
    with pytest.raises(ContractError, match="precision"):
        parse_sql_type("NUMERIC(39,0)")


def test_numeric_scale_greater_than_precision_raises():
    with pytest.raises(ContractError, match="scale"):
        parse_sql_type("NUMERIC(5,6)")


def test_numeric_max_precision_accepted():
    assert parse_sql_type("NUMERIC(38,0)") == SqlType("NUMERIC", precision=38, scale=0)
    assert parse_sql_type("NUMERIC(1,0)") == SqlType("NUMERIC", precision=1, scale=0)
    assert parse_sql_type("NUMERIC(1,1)") == SqlType("NUMERIC", precision=1, scale=1)


def test_double_precision_underscored_form_rejected():
    with pytest.raises(ContractError):
        parse_sql_type("DOUBLE_PRECISION")
    # canonical spelling still parses and produces the underscored base name
    assert parse_sql_type("DOUBLE PRECISION").base == "DOUBLE_PRECISION"


def test_arrow_mapping():
    assert arrow_type(parse_sql_type("NUMERIC(10,2)")) == pa.decimal128(10, 2)
    assert arrow_type(parse_sql_type("VARCHAR(9)")) == pa.string()
    assert arrow_type(parse_sql_type("TIMESTAMP")) == pa.timestamp("us")
    assert arrow_type(parse_sql_type("INT")) == pa.int32()


# continuo_python_runtime.types.parse_sql_type calls
# continuo_validation_contract.types.validate_column_type as its single
# acceptance authority (§ task 8), so the two must never disagree on whether a
# type string is grammar-shaped at all. This vector covers every grammar
# member plus several classic rejects, asserting both accept and both reject
# the same strings -- a regression guard against the two copies drifting.
_GRAMMAR_MEMBERS = [
    "BIGINT",
    "INT",
    "INTEGER",
    "DOUBLE PRECISION",
    "TEXT",
    "TIMESTAMP",
    "DATE",
    "BOOLEAN",
    "NUMERIC(10,2)",
    "DECIMAL(5,0)",
    "VARCHAR(255)",
    "CHAR(1)",
]

_GRAMMAR_REJECTS = ["FLOAT", "VARCHAR", "NUMERIC(10)", "INT4", ""]


@pytest.mark.parametrize("raw", _GRAMMAR_MEMBERS)
def test_contract_and_runtime_agree_on_accepted_grammar(raw):
    validate_column_type(raw)  # must not raise
    parse_sql_type(raw)  # must not raise


@pytest.mark.parametrize("raw", _GRAMMAR_REJECTS)
def test_contract_and_runtime_agree_on_rejected_grammar(raw):
    with pytest.raises(ValueError):
        validate_column_type(raw)
    with pytest.raises(ContractError):
        parse_sql_type(raw)


def test_parse_sql_type_delegates_acceptance_to_contract(monkeypatch):
    """parse_sql_type must call the contract's validate_column_type as its
    single acceptance authority (not just happen to agree with it): a type
    string the contract's function rejects must be rejected here too, even
    if this module's own precision/scale logic would otherwise accept it."""
    import continuo_python_runtime.types as types_mod

    def fake_reject(type_str: str) -> None:
        raise ValueError(f"stubbed rejection of {type_str!r}")

    monkeypatch.setattr(types_mod, "validate_column_type", fake_reject)
    with pytest.raises(ContractError, match="stubbed rejection"):
        types_mod.parse_sql_type("BIGINT")
