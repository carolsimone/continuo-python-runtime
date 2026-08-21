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

from continuo_python_runtime.csv_source import CsvUri, parse_csv_uri
from continuo_python_runtime.csv_readers import reader_for
from continuo_python_runtime.csv_readers.https import HttpsCsvSourceReader
from continuo_python_runtime.csv_readers.s3 import S3CsvSourceReader

CSV_BODY = b"order_id,amount,extra\n1,10.5,x\n2,20.0,y\n"


@pytest.fixture(scope="session")
def minio(minio_container):  # minio_container: session fixture starting minio via docker
    endpoint, access, secret = minio_container
    client = boto3.client(
        "s3", endpoint_url=endpoint,
        aws_access_key_id=access, aws_secret_access_key=secret,
    )
    client.create_bucket(Bucket="drops")
    client.put_object(Bucket="drops", Key="orders.csv", Body=CSV_BODY)
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


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    honour_range = True

    def do_GET(self):
        if self.path == "/orders.csv":
            body = CSV_BODY
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
        else:
            self.send_response(404)
            self.end_headers()

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


def test_reader_for_dispatches_on_scheme():
    assert isinstance(reader_for(parse_csv_uri("s3://b/k")), S3CsvSourceReader)
    assert isinstance(
        reader_for(parse_csv_uri("https://x/y.csv")), HttpsCsvSourceReader)
