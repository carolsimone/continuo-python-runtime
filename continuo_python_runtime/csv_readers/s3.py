"""continuo_python_runtime/csv_readers/s3.py"""
from pathlib import Path

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from continuo_python_runtime.csv_source import (
    HEADER_PROBE_BYTES,
    MAX_HEADER_BYTES,
    CsvSourceReader,
    CsvUri,
    extract_header_line,
)
from continuo_python_runtime.validation.s3 import make_s3_client


class S3CsvSourceReader(CsvSourceReader):
    """Reads a csv source from S3. Reuses make_s3_client so S3_ENDPOINT_URL
    (minio, localstack) and boto3's own credential chain behave identically
    to the validation runner's existing S3 access."""

    def fetch_header_line(self, uri: CsvUri) -> str:
        client = make_s3_client()
        start = 0
        buf = b""
        while True:
            end = start + HEADER_PROBE_BYTES - 1
            try:
                body = client.get_object(
                    Bucket=uri.bucket, Key=uri.key, Range=f"bytes={start}-{end}"
                )["Body"].read()
            except ClientError as exc:
                if start == 0 and exc.response.get("Error", {}).get("Code") == "InvalidRange":
                    # A Range request on byte 0 is unsatisfiable only when the
                    # object itself is 0 bytes long: treat a 0-byte csv source
                    # as an empty header line rather than a hard failure --
                    # the caller (validation/runner.py) raises a clear error
                    # for an empty header line.
                    return ""
                raise
            buf += body
            if b"\n" in buf:
                return extract_header_line(buf, uri.raw)
            if len(body) < HEADER_PROBE_BYTES:  # whole object read, no newline
                return buf.rstrip(b"\r").decode("utf-8-sig")
            if len(buf) > MAX_HEADER_BYTES:
                raise ValueError(
                    f"csv header line exceeds {MAX_HEADER_BYTES} bytes: {uri.raw}")
            start += HEADER_PROBE_BYTES

    def fetch(self, uri: CsvUri, dest: Path) -> Path:
        client = make_s3_client()
        with open(dest, "wb") as f:
            client.download_fileobj(uri.bucket, uri.key, f)
        return dest


assert issubclass(S3CsvSourceReader, CsvSourceReader)
