"""tests/test_csv_readers_https.py — unit tier: HttpsCsvSourceReader mechanics
that don't need a live server (redirect-scheme enforcement, timeout wiring).
See test_csv_readers_integration.py for the live-server-backed Range/stream
behavior tests, including an end-to-end redirect-downgrade test against a
reachable target."""
import urllib.error
import urllib.request

import pytest

from continuo_python_runtime.csv_readers import https as https_mod
from continuo_python_runtime.csv_readers.https import (
    _HttpsOnlyRedirectHandler,
    _TIMEOUT_SECONDS,
    HttpsCsvSourceReader,
)
from continuo_python_runtime.csv_source import CsvUri


@pytest.mark.parametrize("newurl", [
    "http://example.com/orders.csv",
    "HTTP://example.com/orders.csv",
    "ftp://example.com/orders.csv",
])
def test_redirect_handler_rejects_non_https_target(newurl):
    handler = _HttpsOnlyRedirectHandler()
    req = urllib.request.Request("https://example.com/orders.csv")
    with pytest.raises(urllib.error.HTTPError, match="non-https"):
        handler.redirect_request(req, None, 302, "Found", {}, newurl)


def test_redirect_handler_allows_https_target(monkeypatch):
    handler = _HttpsOnlyRedirectHandler()
    req = urllib.request.Request("https://example.com/orders.csv")
    captured = {}

    def fake_super_redirect(self, req, fp, code, msg, headers, newurl):
        captured["newurl"] = newurl
        return "a-request-object"

    monkeypatch.setattr(
        urllib.request.HTTPRedirectHandler, "redirect_request", fake_super_redirect)

    result = handler.redirect_request(
        req, None, 302, "Found", {}, "https://example.com/y.csv")

    assert result == "a-request-object"
    assert captured["newurl"] == "https://example.com/y.csv"


def test_timeout_constant_is_finite_and_positive():
    assert 0 < _TIMEOUT_SECONDS < 300


class _FakeHttpResponse:
    """Just enough of urllib's response object for fetch_header_line/fetch:
    a context manager with .status and a chunked .read(size)."""

    status = 200

    def __init__(self, body: bytes):
        self._remaining = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk, self._remaining = self._remaining, b""
        else:
            chunk, self._remaining = self._remaining[:size], self._remaining[size:]
        return chunk


def test_fetch_header_line_passes_the_timeout_to_opener_open(monkeypatch):
    captured = {}

    def fake_open(req, timeout=None):
        captured["timeout"] = timeout
        return _FakeHttpResponse(b"order_id,amount\n1,2\n")

    monkeypatch.setattr(https_mod._opener, "open", fake_open)

    uri = CsvUri(scheme="https", raw="https://example.com/orders.csv")
    header = HttpsCsvSourceReader().fetch_header_line(uri)

    assert header == "order_id,amount"
    assert captured["timeout"] == _TIMEOUT_SECONDS


def test_fetch_passes_the_timeout_to_opener_open(monkeypatch, tmp_path):
    captured = {}

    def fake_open(url, timeout=None):
        captured["timeout"] = timeout
        return _FakeHttpResponse(b"order_id,amount\n1,2\n")

    monkeypatch.setattr(https_mod._opener, "open", fake_open)

    uri = CsvUri(scheme="https", raw="https://example.com/orders.csv")
    dest = HttpsCsvSourceReader().fetch(uri, tmp_path / "o.csv")

    assert dest.read_bytes() == b"order_id,amount\n1,2\n"
    assert captured["timeout"] == _TIMEOUT_SECONDS
