import pyarrow as pa
import pytest

from continuo_python_runtime.conform import conform, to_arrow
from continuo_python_runtime.contract.model import Column
from continuo_python_runtime.errors import ConformError, ScriptError

COLS = (
    Column("id", "INTEGER", nullable=False),
    Column("amount", "NUMERIC(10,2)"),
    Column("email", "VARCHAR(5)"),
)


def _t(**cols):
    return pa.table(dict(cols))


def test_happy_path_reorders_and_casts():
    out = conform(_t(email=["a"], amount=["1.50"], id=[1]), COLS)
    assert out.column_names == ["id", "amount", "email"]
    assert out.schema.field("id").type == pa.int32()
    assert out.schema.field("amount").type == pa.decimal128(10, 2)


def test_extra_column_raises_by_default():
    with pytest.raises(ConformError, match="undeclared column"):
        conform(_t(id=[1], amount=["1"], email=["a"], tmp=[0]), COLS)


def test_extra_column_warn_drops(caplog):
    with caplog.at_level("WARNING"):
        out = conform(
            _t(id=[1], amount=["1"], email=["a"], tmp=[0]), COLS, extra_columns="warn"
        )
    assert "tmp" not in out.column_names
    assert any("dropping undeclared" in r.message for r in caplog.records)


def test_missing_column_raises():
    with pytest.raises(ConformError, match="missing column"):
        conform(_t(id=[1], amount=["1"]), COLS)


def test_lossy_float_to_int_raises():
    with pytest.raises(ConformError):
        conform(_t(id=[3.9], amount=["1"], email=["a"]), COLS)


def test_unparseable_string_raises():
    with pytest.raises(ConformError):
        conform(_t(id=["abc"], amount=["1"], email=["a"]), COLS)


def test_decimal_overflow_raises():
    with pytest.raises(ConformError):
        conform(_t(id=[1], amount=["123456789.12"], email=["a"]), COLS)


def test_varchar_overflow_raises():
    with pytest.raises(ConformError, match="email.*exceeds VARCHAR\\(5\\)"):
        conform(_t(id=[1], amount=["1"], email=["toolong"]), COLS)


def test_null_in_not_null_column_raises():
    with pytest.raises(ConformError, match="id.*null"):
        conform(_t(id=[None], amount=["1"], email=["a"]), COLS)


def test_to_arrow_rejects_non_convertible():
    with pytest.raises(ScriptError):
        to_arrow(object())


def test_to_arrow_passthrough_and_capsule():
    t = _t(id=[1])
    assert to_arrow(t) is t
