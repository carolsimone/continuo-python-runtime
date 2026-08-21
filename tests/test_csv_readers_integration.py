"""tests/test_csv_readers_integration.py

Real-backend tests: minio for S3CsvSourceReader, a live stdlib HTTP server for
HttpsCsvSourceReader. HTTPS-the-scheme is terminated before our code in
production (urllib handles TLS); the adapter's Range/stream logic is what these
tests exercise, so the local server speaking plain HTTP to a loopback socket is
acceptable ONLY here — build the CsvUri directly rather than via parse_csv_uri.
"""
import http.server
import socketserver
import threading

import boto3
import pytest

from continuo_python_runtime.csv_source import (
    HEADER_PROBE_BYTES,
    MAX_HEADER_BYTES,
    CsvUri,
    parse_csv_uri,
)
from continuo_python_runtime.csv_readers import reader_for
from continuo_python_runtime.csv_readers.https import HttpsCsvSourceReader
from continuo_python_runtime.csv_readers.s3 import S3CsvSourceReader

pytestmark = pytest.mark.integration

CSV_BODY = b"order_id,amount,extra\n1,10.5,x\n2,20.0,y\n"


def _long_header_body(header_len: int) -> tuple[bytes, str]:
    """A csv body whose header line is exactly `header_len` bytes (no
    newline inside it), followed by one short data row. Returns
    (full_body, expected_header_str) so a test can assert on the exact
    text a multi-probe fetch_header_line should reassemble."""
    chunk = "colname_padding_"
    header = (chunk * (header_len // len(chunk) + 1))[:header_len]
    return header.encode() + b"\n1,2,3\n", header


# 2.5x HEADER_PROBE_BYTES: a Range-honouring reader needs 3 probes to see
# the newline, so this is exactly the case FIX 1 (silent truncation to the
# first ~64KB window) would have gotten wrong.
LONG_HEADER_LEN = HEADER_PROBE_BYTES * 5 // 2
LONG_HEADER_BODY, LONG_HEADER_STR = _long_header_body(LONG_HEADER_LEN)

# No newline anywhere, longer than MAX_HEADER_BYTES: must raise, never hang
# or return a truncated line.
OVERFLOW_BODY = b"z" * (MAX_HEADER_BYTES + 100_000)

# No newline anywhere, sized strictly between HEADER_PROBE_BYTES and
# MAX_HEADER_BYTES. A server that ignores Range and re-serves this whole
# body on every retry must recognize its first response as final (via the
# response status) rather than re-appending it pass after pass, which would
# otherwise double the buffer past MAX_HEADER_BYTES and raise a false
# overflow (FIX 1).
NO_NEWLINE_MID_BODY = b"a" * 100_000

# A header line that DOES terminate in a newline, but only after exceeding
# MAX_HEADER_BYTES. Unlike OVERFLOW_BODY (no newline anywhere), this exercises
# the ordering bug where a newline arriving inside an already-oversized buffer
# was returned as a successful (if oversized) header instead of being
# rejected: the length check must run on the resolved line itself, not only
# on an intermediate buffer length reached before any newline was seen.
OVERSIZED_WITH_NEWLINE_LEN = MAX_HEADER_BYTES + 50_000
OVERSIZED_WITH_NEWLINE_BODY, _ = _long_header_body(OVERSIZED_WITH_NEWLINE_LEN)

# A UTF-8 byte-order mark prefixed onto an otherwise-ordinary header: the
# reader must strip it so the first declared column name still matches.
BOM_CSV_BODY = b"\xef\xbb\xbf" + CSV_BODY
BOM_HEADER_STR = "order_id,amount,extra"


@pytest.fixture(scope="session")
def minio(minio_container):  # minio_container: session fixture starting minio via docker
    endpoint, access, secret = minio_container
    client = boto3.client(
        "s3", endpoint_url=endpoint,
        aws_access_key_id=access, aws_secret_access_key=secret,
    )
    client.create_bucket(Bucket="drops")
    client.put_object(Bucket="drops", Key="orders.csv", Body=CSV_BODY)
    client.put_object(Bucket="drops", Key="long_header.csv", Body=LONG_HEADER_BODY)
    client.put_object(Bucket="drops", Key="overflow.csv", Body=OVERFLOW_BODY)
    client.put_object(
        Bucket="drops", Key="oversized_with_newline.csv", Body=OVERSIZED_WITH_NEWLINE_BODY)
    client.put_object(Bucket="drops", Key="bom.csv", Body=BOM_CSV_BODY)
    return endpoint


def test_s3_fetch_header_line(minio, monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT_URL", minio)
    header = S3CsvSourceReader().fetch_header_line(parse_csv_uri("s3://drops/orders.csv"))
    assert header == "order_id,amount,extra"


def test_s3_fetch_full_object(minio, monkeypatch, tmp_path):
    monkeypatch.setenv("S3_ENDPOINT_URL", minio)
    dest = S3CsvSourceReader().fetch(
        parse_csv_uri("s3://drops/orders.csv"), tmp_path / "o.csv")
    assert dest.read_bytes() == CSV_BODY


def test_s3_missing_object_raises(minio, monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT_URL", minio)
    with pytest.raises(Exception):
        S3CsvSourceReader().fetch_header_line(parse_csv_uri("s3://drops/nope.csv"))


def test_s3_fetch_header_line_extends_across_probes(minio, monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT_URL", minio)
    header = S3CsvSourceReader().fetch_header_line(
        parse_csv_uri("s3://drops/long_header.csv"))
    assert header == LONG_HEADER_STR


def test_s3_fetch_header_line_overflow_raises(minio, monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT_URL", minio)
    with pytest.raises(Exception):
        S3CsvSourceReader().fetch_header_line(parse_csv_uri("s3://drops/overflow.csv"))


def test_s3_fetch_header_line_oversized_line_with_late_newline_raises(minio, monkeypatch):
    """A newline that only shows up after the accumulated buffer already
    exceeds MAX_HEADER_BYTES must still raise -- not be returned as a
    successful (oversized) header line."""
    monkeypatch.setenv("S3_ENDPOINT_URL", minio)
    with pytest.raises(ValueError, match="exceeds"):
        S3CsvSourceReader().fetch_header_line(
            parse_csv_uri("s3://drops/oversized_with_newline.csv"))


def test_s3_fetch_header_line_strips_utf8_bom(minio, monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT_URL", minio)
    header = S3CsvSourceReader().fetch_header_line(parse_csv_uri("s3://drops/bom.csv"))
    assert header == BOM_HEADER_STR
    assert header[0] != "﻿"


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    honour_range = True

    _BODIES = {
        "/orders.csv": CSV_BODY,
        "/long_header.csv": LONG_HEADER_BODY,
        "/overflow.csv": OVERFLOW_BODY,
        "/no_newline.csv": NO_NEWLINE_MID_BODY,
        "/oversized_with_newline.csv": OVERSIZED_WITH_NEWLINE_BODY,
        "/bom.csv": BOM_CSV_BODY,
    }

    def do_GET(self):
        body = self._BODIES.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        rng = self.headers.get("Range")
        if rng and self.honour_range:
            start, end = rng.removeprefix("bytes=").split("-")
            body = body[int(start):int(end) + 1]
            self.send_response(206)
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep test output clean
        pass


@pytest.fixture(params=[True, False], ids=["range-honoured", "range-ignored"])
def http_csv_server(request):
    handler = type("H", (_RangeHandler,), {"honour_range": request.param})
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as srv:
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        yield f"127.0.0.1:{srv.server_address[1]}"
        srv.shutdown()


def test_https_fetch_header_line_with_and_without_range(http_csv_server):
    uri = CsvUri(scheme="https", raw=f"http://{http_csv_server}/orders.csv")
    header = HttpsCsvSourceReader().fetch_header_line(uri)
    assert header == "order_id,amount,extra"


def test_https_fetch_full(http_csv_server, tmp_path):
    uri = CsvUri(scheme="https", raw=f"http://{http_csv_server}/orders.csv")
    dest = HttpsCsvSourceReader().fetch(uri, tmp_path / "o.csv")
    assert dest.read_bytes() == CSV_BODY


def test_https_404_raises(http_csv_server):
    uri = CsvUri(scheme="https", raw=f"http://{http_csv_server}/nope.csv")
    with pytest.raises(Exception):
        HttpsCsvSourceReader().fetch_header_line(uri)


def test_https_fetch_header_line_extends_across_probes(http_csv_server):
    uri = CsvUri(scheme="https", raw=f"http://{http_csv_server}/long_header.csv")
    header = HttpsCsvSourceReader().fetch_header_line(uri)
    assert header == LONG_HEADER_STR


def test_https_fetch_header_line_overflow_raises(http_csv_server):
    uri = CsvUri(scheme="https", raw=f"http://{http_csv_server}/overflow.csv")
    with pytest.raises(Exception):
        HttpsCsvSourceReader().fetch_header_line(uri)


def test_https_fetch_header_line_no_newline_mid_size_is_not_a_false_overflow(http_csv_server):
    """A no-newline body sized between HEADER_PROBE_BYTES and MAX_HEADER_BYTES
    must come back as the header line verbatim, on both a Range-honouring
    server (multi-probe extend, whole object read on the short final probe)
    and a Range-ignoring one (FIX 1: the 200 response is terminal on the
    first pass, never re-appended)."""
    uri = CsvUri(scheme="https", raw=f"http://{http_csv_server}/no_newline.csv")
    header = HttpsCsvSourceReader().fetch_header_line(uri)
    assert header == NO_NEWLINE_MID_BODY.decode("utf-8")


def test_https_fetch_header_line_oversized_line_with_late_newline_raises(http_csv_server):
    """A newline that only shows up after the accumulated buffer already
    exceeds MAX_HEADER_BYTES must still raise -- not be returned as a
    successful (oversized) header line. Exercises both the range-honoured
    (multi-probe accumulation) and range-ignored (single bounded read) paths."""
    uri = CsvUri(scheme="https", raw=f"http://{http_csv_server}/oversized_with_newline.csv")
    with pytest.raises(ValueError, match="exceeds"):
        HttpsCsvSourceReader().fetch_header_line(uri)


def test_https_fetch_header_line_strips_utf8_bom(http_csv_server):
    uri = CsvUri(scheme="https", raw=f"http://{http_csv_server}/bom.csv")
    header = HttpsCsvSourceReader().fetch_header_line(uri)
    assert header == BOM_HEADER_STR
    assert header[0] != "﻿"


class _DowngradeTargetHandler(http.server.BaseHTTPRequestHandler):
    """A REACHABLE plain-HTTP server standing in for a downgrade target. Using
    a reachable target (rather than a nonexistent host) makes the redirect
    tests below meaningful: the vulnerable (pre-fix) code doesn't merely fail
    to connect, it actually follows the redirect and successfully returns
    this server's content -- proving the redirect really would have been
    followed, not just that *some* exception happened to occur."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(CSV_BODY)))
        self.end_headers()
        self.wfile.write(CSV_BODY)

    def log_message(self, *args):
        pass


@pytest.fixture
def downgrade_redirect_uri():
    """An https:// (test-convention) source whose every request 302s to a
    second, reachable, genuinely plain-HTTP server."""
    with socketserver.TCPServer(("127.0.0.1", 0), _DowngradeTargetHandler) as target:
        tt = threading.Thread(target=target.serve_forever, daemon=True)
        tt.start()
        target_port = target.server_address[1]

        redirector = type("Redirector", (http.server.BaseHTTPRequestHandler,), {
            "do_GET": lambda self: (
                self.send_response(302),
                self.send_header("Location", f"http://127.0.0.1:{target_port}/orders.csv"),
                self.end_headers(),
            ),
            "log_message": lambda self, *a: None,
        })
        with socketserver.TCPServer(("127.0.0.1", 0), redirector) as src:
            ts = threading.Thread(target=src.serve_forever, daemon=True)
            ts.start()
            yield f"http://127.0.0.1:{src.server_address[1]}/redirect.csv"
            src.shutdown()
        target.shutdown()


def test_https_fetch_header_line_rejects_redirect_to_non_https(downgrade_redirect_uri):
    """An https:// source that redirects to a REACHABLE non-https target must
    not be followed -- the pre-fix code would have silently downgraded to
    plaintext and returned the target's header line instead of raising."""
    uri = CsvUri(scheme="https", raw=downgrade_redirect_uri)
    with pytest.raises(Exception):
        HttpsCsvSourceReader().fetch_header_line(uri)


def test_https_fetch_rejects_redirect_to_non_https(downgrade_redirect_uri, tmp_path):
    uri = CsvUri(scheme="https", raw=downgrade_redirect_uri)
    with pytest.raises(Exception):
        HttpsCsvSourceReader().fetch(uri, tmp_path / "o.csv")


def test_reader_for_dispatches_on_scheme():
    assert isinstance(reader_for(parse_csv_uri("s3://b/k")), S3CsvSourceReader)
    assert isinstance(
        reader_for(parse_csv_uri("https://x/y.csv")), HttpsCsvSourceReader)
