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


def test_duplicate_column_raises():
    dup_table = pa.Table.from_arrays([pa.array([1]), pa.array([2])], names=["id", "id"])
    with pytest.raises(ConformError, match="duplicate column"):
        conform(dup_table, COLS)


def test_invalid_extra_columns_policy_raises():
    with pytest.raises(ValueError):
        conform(_t(id=[1], amount=["1"], email=["a"]), COLS, extra_columns="ignore")


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


# --- Finding 1: lossy cast pairs must be rejected before Arrow cast ---


def test_float_to_decimal_raises_conform_error():
    cols = (Column("amount", "NUMERIC(10,2)"),)
    with pytest.raises(ConformError, match="amount"):
        conform(_t(amount=[1.234]), cols)


def test_int_to_boolean_raises_conform_error():
    cols = (Column("flag", "BOOLEAN"),)
    with pytest.raises(ConformError, match="flag"):
        conform(_t(flag=[1]), cols)


def test_float_to_boolean_raises_conform_error():
    cols = (Column("flag", "BOOLEAN"),)
    with pytest.raises(ConformError, match="flag"):
        conform(_t(flag=[1.0]), cols)


def test_timestamp_to_date_raises_conform_error():
    import datetime

    cols = (Column("d", "DATE"),)
    with pytest.raises(ConformError, match="d"):
        conform(_t(d=[datetime.datetime(2020, 1, 1, 13, 45)]), cols)


def test_boolean_to_boolean_still_allowed():
    cols = (Column("flag", "BOOLEAN"),)
    out = conform(_t(flag=[True, False]), cols)
    assert out.column("flag").to_pylist() == [True, False]


def test_date_to_date_still_allowed():
    import datetime

    cols = (Column("d", "DATE"),)
    out = conform(_t(d=[datetime.date(2020, 1, 1)]), cols)
    assert out.column("d").to_pylist() == [datetime.date(2020, 1, 1)]


def test_int_to_int64_widening_still_allowed():
    cols = (Column("id", "BIGINT"),)
    out = conform(_t(id=pa.array([1, 2, 3], type=pa.int32())), cols)
    assert out.column("id").to_pylist() == [1, 2, 3]


def test_string_to_decimal_overflow_still_raises():
    cols = (Column("amount", "NUMERIC(5,2)"),)
    with pytest.raises(ConformError):
        conform(_t(amount=["123456.78"]), cols)


def test_string_to_date_parse_error_still_raises():
    cols = (Column("d", "DATE"),)
    with pytest.raises(ConformError):
        conform(_t(d=["not-a-date"]), cols)


def test_integer_to_decimal_still_allowed():
    cols = (Column("amount", "NUMERIC(21,2)"),)
    out = conform(_t(amount=pa.array([123], type=pa.int64())), cols)
    assert out.column("amount").to_pylist() == [__import__("decimal").Decimal("123.00")]


def test_string_to_decimal_still_allowed():
    cols = (Column("amount", "NUMERIC(10,2)"),)
    out = conform(_t(amount=["1.23"]), cols)
    assert out.column("amount").to_pylist() == [__import__("decimal").Decimal("1.23")]


def test_decimal_to_decimal_still_allowed():
    from decimal import Decimal

    cols = (Column("amount", "NUMERIC(10,2)"),)
    out = conform(_t(amount=pa.array([Decimal("1.5")], type=pa.decimal128(5, 1))), cols)
    assert out.column("amount").to_pylist() == [Decimal("1.50")]


# --- Finding 4: declared nullability must be restored on the returned schema ---


def test_returned_schema_restores_declared_nullability():
    out = conform(_t(id=[1], amount=["1.50"], email=["a"]), COLS)
    assert out.schema.field("id").nullable is False
    assert out.schema.field("amount").nullable is True
    assert out.schema.field("email").nullable is True


# --- Finding 2: pandas duck-typed DataFrame must be detected before the
# __arrow_c_stream__ branch, since pandas 3.x DataFrames implement both. ---


def test_to_arrow_prefers_pandas_branch_over_arrow_c_stream(monkeypatch):
    """Simulates a pandas-3.x-like object exposing both a pandas duck-type
    fingerprint and __arrow_c_stream__ (as real pandas 3.x DataFrames do).
    pandas itself is not installed in this venv, so a fake 'pandas' module is
    injected into sys.modules to make the lazy `import pandas` succeed, and
    `pyarrow.Table.from_pandas` is mocked so we don't need a real DataFrame.
    This verifies the duck-typed pandas branch is tried *before* the generic
    __arrow_c_stream__ branch, per the ordering fix for Finding 2 -- the
    fake object's __arrow_c_stream__ must never be invoked.
    """
    import sys
    from unittest import mock

    calls = []

    class FakeDataFrame:
        def __arrow_c_stream__(self, requested_schema=None):
            calls.append("arrow_c_stream")
            raise AssertionError(
                "generic __arrow_c_stream__ branch must not run for pandas-like objects"
            )

    FakeDataFrame.__module__ = "pandas.core.frame"
    FakeDataFrame.__qualname__ = "DataFrame"
    FakeDataFrame.__name__ = "DataFrame"

    obj = FakeDataFrame()

    fake_pandas_module = mock.MagicMock()
    monkeypatch.setitem(sys.modules, "pandas", fake_pandas_module)

    sentinel_table = _t(id=[1])
    from_pandas_mock = mock.Mock(return_value=sentinel_table)

    # pyarrow.Table is a Cython extension type: its attributes can't be
    # patched directly (immutable type), so rebind the module-level `Table`
    # name on the `pyarrow` package to a stand-in class instead. `to_arrow`
    # still does `isinstance(obj, pa.Table)` first, which correctly
    # evaluates to False for our fake pandas object against this stand-in.
    class FakeTable:
        from_pandas = staticmethod(from_pandas_mock)

    monkeypatch.setattr(pa, "Table", FakeTable)

    result = to_arrow(obj)

    from_pandas_mock.assert_called_once_with(obj, preserve_index=False)
    assert result is sentinel_table
    assert calls == []
