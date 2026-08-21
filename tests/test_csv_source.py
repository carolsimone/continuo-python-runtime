"""tests/test_csv_source.py"""
import pytest

from continuo_python_runtime.csv_source import (
    CsvSourceReader,
    check_header,
    parse_csv_uri,
)


def test_parse_s3_uri():
    uri = parse_csv_uri("s3://drops/exports/orders.csv")
    assert (uri.scheme, uri.bucket, uri.key) == ("s3", "drops", "exports/orders.csv")
    assert uri.raw == "s3://drops/exports/orders.csv"


def test_parse_https_uri():
    uri = parse_csv_uri("https://example.com/x.csv")
    assert uri.scheme == "https"
    assert uri.raw == "https://example.com/x.csv"


@pytest.mark.parametrize("bad", [
    "http://example.com/x.csv",   # http is not accepted
    "s3://bucket-only",
    "s3://bucket/",
    "gs://bucket/key",
    "https:///x.csv",              # empty host not accepted
    "https://",                     # empty host not accepted
    "orders.csv",
    "",
])
def test_parse_rejects_invalid(bad):
    with pytest.raises(ValueError):
        parse_csv_uri(bad)


def test_check_header_presence_only_any_order():
    extras = check_header(["b", "a", "c"], ["a", "b"])
    assert extras == {"c"}


def test_check_header_no_extras():
    assert check_header(["a", "b"], ["a", "b"]) == set()


def test_check_header_missing_declared_column_raises():
    with pytest.raises(ValueError, match="missing declared column"):
        check_header(["a"], ["a", "b"])


def test_reader_port_is_abstract():
    with pytest.raises(TypeError):
        CsvSourceReader()  # type: ignore[abstract]
