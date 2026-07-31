import pyarrow as pa
import pytest

from continuo_python_runtime.context import RunContext
from continuo_python_runtime.errors import ReadError


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def fetch(self, sql):
        self.calls.append(sql)
        return pa.table({"id": [1]})


def test_read_resolves_declared_sql_and_memoizes(node_fixture):
    ad = FakeAdapter()
    ctx = RunContext(node_fixture, ad)
    t1, t2 = ctx.read("ids"), ctx.read("ids")
    assert t1 is t2
    assert ad.calls == ["select id from analytics.a"]


def test_unknown_name_raises_without_adapter_call(node_fixture):
    ad = FakeAdapter()
    with pytest.raises(ReadError, match="undeclared read 'nope'.*declared: \\['ids'\\]"):
        RunContext(node_fixture, ad).read("nope")
    assert ad.calls == []


def test_adapter_failure_wrapped(node_fixture):
    class Boom:
        def fetch(self, sql):
            raise RuntimeError("db down")
    with pytest.raises(ReadError, match="'ids' failed: db down"):
        RunContext(node_fixture, Boom()).read("ids")
