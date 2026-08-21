"""continuo_python_runtime/csv_readers/https.py"""
import shutil
import urllib.request
from pathlib import Path

from continuo_python_runtime.csv_source import (
    HEADER_PROBE_BYTES,
    MAX_HEADER_BYTES,
    CsvSourceReader,
    CsvUri,
)


class HttpsCsvSourceReader(CsvSourceReader):
    """Reads a csv source over HTTPS (public URLs; no auth in v1). Mirrors
    S3CsvSourceReader's probe-and-extend strategy: each ranged request is
    independent, so a server that ignores Range and answers 200 (not 206)
    is handled too -- its response is the whole object, so it is treated
    as terminal on the first pass regardless of whether it contains a
    newline or how large it is, rather than being re-fetched and
    re-appended pass after pass."""

    def fetch_header_line(self, uri: CsvUri) -> str:
        start = 0
        buf = b""
        while True:
            end = start + HEADER_PROBE_BYTES - 1
            req = urllib.request.Request(
                uri.raw, headers={"Range": f"bytes={start}-{end}"})
            with urllib.request.urlopen(req) as resp:  # noqa: S310 — scheme gated by parse_csv_uri
                body = resp.read()
                range_honoured = resp.status == 206
            if not range_honoured:
                # The server ignored our Range header and returned the entire
                # object (status 200), not just the requested window -- *body*
                # is therefore the whole object and this response is terminal,
                # regardless of its size relative to HEADER_PROBE_BYTES. Every
                # retry would re-fetch the identical full body, so looping
                # would only re-append it pass after pass and eventually trip
                # a false MAX_HEADER_BYTES overflow.
                if b"\n" in body:
                    return body.split(b"\n", 1)[0].rstrip(b"\r").decode("utf-8")
                if len(body) > MAX_HEADER_BYTES:
                    raise ValueError(
                        f"csv header line exceeds {MAX_HEADER_BYTES} bytes: {uri.raw}")
                return body.rstrip(b"\r").decode("utf-8")
            buf += body
            if b"\n" in buf:
                return buf.split(b"\n", 1)[0].rstrip(b"\r").decode("utf-8")
            if len(body) < HEADER_PROBE_BYTES:  # whole object read, no newline
                return buf.rstrip(b"\r").decode("utf-8")
            if len(buf) > MAX_HEADER_BYTES:
                raise ValueError(
                    f"csv header line exceeds {MAX_HEADER_BYTES} bytes: {uri.raw}")
            start += HEADER_PROBE_BYTES

    def fetch(self, uri: CsvUri, dest: Path) -> Path:
        with urllib.request.urlopen(uri.raw) as resp, open(dest, "wb") as f:  # noqa: S310
            shutil.copyfileobj(resp, f)
        return dest


assert issubclass(HttpsCsvSourceReader, CsvSourceReader)
