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
    """Reads a csv source over HTTPS (public URLs; no auth in v1). A server
    that ignores Range and answers 200 is handled by streaming and reading
    only to the first newline before closing the connection."""

    def fetch_header_line(self, uri: CsvUri) -> str:
        req = urllib.request.Request(
            uri.raw, headers={"Range": f"bytes=0-{HEADER_PROBE_BYTES - 1}"})
        buf = b""
        with urllib.request.urlopen(req) as resp:  # noqa: S310 — scheme gated by parse_csv_uri
            while b"\n" not in buf:
                chunk = resp.read(HEADER_PROBE_BYTES)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_HEADER_BYTES:
                    raise ValueError(
                        f"csv header line exceeds {MAX_HEADER_BYTES} bytes: {uri.raw}")
        return buf.split(b"\n", 1)[0].rstrip(b"\r").decode("utf-8")

    def fetch(self, uri: CsvUri, dest: Path) -> Path:
        with urllib.request.urlopen(uri.raw) as resp, open(dest, "wb") as f:  # noqa: S310
            shutil.copyfileobj(resp, f)
        return dest


assert issubclass(HttpsCsvSourceReader, CsvSourceReader)
