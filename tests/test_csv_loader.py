"""tests/test_csv_loader.py — unit tier: the loader through a local-file test
double of the PORT (the port is ours; substituting a test implementation of
our own abstraction is not stubbing an external service)."""
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
