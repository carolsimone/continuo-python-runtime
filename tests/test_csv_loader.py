"""tests/test_csv_loader.py — unit tier: the loader through a local-file test
double of the PORT (the port is ours; substituting a test implementation of
our own abstraction is not stubbing an external service)."""
from decimal import Decimal

import pyarrow as pa
import pytest

from continuo_python_runtime.contract.model import Column, Node
from continuo_python_runtime.csv_loader import produce_csv
from continuo_python_runtime.csv_source import CsvSourceReader
from continuo_python_runtime.errors import LoadError


class LocalFileReader(CsvSourceReader):
    def __init__(self, body: bytes):
        self.body = body

    def fetch_header_line(self, uri):
        return self.body.split(b"\n", 1)[0].decode()

    def fetch(self, uri, dest):
        dest.write_bytes(self.body)
        return dest


def _csv_node(**over):
    kw = dict(
        schema="analytics", table="orders_csv", owner="t", schedule="daily",
        criticality="SECONDARY", script="", kind="python-csv",
        reads={"csv": "s3://drops/orders.csv"},
        output_columns=(
            Column(name="order_id", type="INTEGER", nullable=False),
            Column(name="amount", type="DOUBLE PRECISION"),
        ),
        extra_columns="warn",
    )
    kw.update(over)
    return Node(**kw)


def test_produce_csv_returns_arrow_table():
    table = produce_csv(_csv_node(), reader=LocalFileReader(
        b"order_id,amount\n1,10.5\n2,20.0\n"))
    assert isinstance(table, pa.Table)
    assert table.num_rows == 2


def test_produce_csv_fetch_failure_maps_to_load_error():
    class Broken(CsvSourceReader):
        def fetch_header_line(self, uri):
            raise OSError("boom")

        def fetch(self, uri, dest):
            raise OSError("boom")

    with pytest.raises(LoadError, match="csv fetch failed"):
        produce_csv(_csv_node(), reader=Broken())


def test_produce_csv_logs_structured_warning_for_undeclared_columns(caplog):
    """FIX 7 — spec parity: when the csv carries a column not declared in
    output_columns, the RUN path logs the same structured
    csv_header_extra_columns warning the validation runner emits, so
    extra_columns: drop's silent-discard behavior is observable in both
    places, not just at validation time."""
    node = _csv_node()  # declares only order_id, amount
    with caplog.at_level("WARNING"):
        produce_csv(node, reader=LocalFileReader(
            b"order_id,amount,extra\n1,10.5,x\n2,20.0,y\n"))

    assert "csv_header_extra_columns" in caplog.text
    assert node.relation in caplog.text
    assert "extra" in caplog.text


def test_produce_csv_no_warning_when_all_columns_declared(caplog):
    """No extras: no csv_header_extra_columns warning is logged."""
    with caplog.at_level("WARNING"):
        produce_csv(_csv_node(), reader=LocalFileReader(
            b"order_id,amount\n1,10.5\n2,20.0\n"))

    assert "csv_header_extra_columns" not in caplog.text


def test_produce_csv_preserves_lexical_value_for_declared_varchar_column():
    """A declared VARCHAR column must be read as text -- pyarrow's default
    type inference on `00123` would infer int64, and conform() would then
    write back "123", silently dropping the leading zeros. Passing the
    declared output_columns as read_csv's convert schema (spec: 'output_columns
    names/types as the convert schema') prevents that: the column is parsed as
    a string in the first place, so no lossy int64 round-trip ever happens."""
    node = _csv_node(output_columns=(
        Column(name="order_id", type="VARCHAR(20)", nullable=False),
        Column(name="amount", type="DOUBLE PRECISION"),
    ))
    table = produce_csv(node, reader=LocalFileReader(
        b"order_id,amount\n00123,10.5\n00456,20.0\n"))

    assert table.column("order_id").to_pylist() == ["00123", "00456"]


def test_produce_csv_declared_decimal_column_not_corrupted_by_float_inference():
    """A declared NUMERIC column must not be inferred as float64 first --
    conform()'s own lossy-cast guard rejects float->decimal casts outright,
    so without an explicit convert schema this would fail conform() even for
    a value that is a perfectly valid decimal."""
    node = _csv_node(output_columns=(
        Column(name="order_id", type="INTEGER", nullable=False),
        Column(name="amount", type="NUMERIC(10,2)"),
    ))
    table = produce_csv(node, reader=LocalFileReader(
        b"order_id,amount\n1,10.50\n2,20.00\n"))

    assert pa.types.is_decimal(table.schema.field("amount").type)
    assert table.column("amount").to_pylist() == [Decimal("10.50"), Decimal("20.00")]
