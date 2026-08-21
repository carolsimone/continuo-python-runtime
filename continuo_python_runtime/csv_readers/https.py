"""continuo_python_runtime/csv_readers/https.py"""
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from continuo_python_runtime.csv_source import (
    HEADER_PROBE_BYTES,
    MAX_HEADER_BYTES,
    CsvSourceReader,
    CsvUri,
    extract_header_line,
)

# Both urlopen calls below must never hang forever: a source that accepts the
# TCP connection but stalls on headers or body would otherwise wedge a
# validation run or a scheduled node run indefinitely.
_TIMEOUT_SECONDS = 30

# Chunk size for the bounded read used when a server ignores our Range header
# and answers 200: reading in chunks this small lets fetch_header_line stop
# at the first newline (or MAX_HEADER_BYTES) without ever buffering a
# multi-gigabyte body just to inspect its first line.
_READ_CHUNK_BYTES = 65_536


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuses to follow a redirect whose target is not itself https://.

    urllib follows redirects automatically, and by default does not care
    what scheme the target uses -- an https:// source that redirects to
    http:// (or any other scheme) would otherwise silently downgrade both
    the header probe and the full fetch to plaintext, defeating
    parse_csv_uri's https-only restriction.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        if not newurl.lower().startswith("https://"):
            raise urllib.error.HTTPError(
                newurl, code,
                f"refusing to follow redirect to non-https URL: {newurl}",
                headers, fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HttpsOnlyRedirectHandler)


def _read_bounded(resp, limit: int) -> bytes:
    """Read ``resp`` in small chunks, stopping at the first newline or once
    more than ``limit`` bytes have been buffered, whichever comes first.

    Used for the range-ignored (status 200) path, where the server's
    response is the whole object: an unbounded ``resp.read()`` there would
    buffer a multi-gigabyte body in full just to read its header line.
    """
    buf = b""
    while True:
        chunk = resp.read(_READ_CHUNK_BYTES)
        if not chunk:
            return buf
        buf += chunk
        if b"\n" in buf or len(buf) > limit:
            return buf


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
            with _opener.open(req, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310 — scheme gated by parse_csv_uri and _HttpsOnlyRedirectHandler
                range_honoured = resp.status == 206
                if range_honoured:
                    body = resp.read()
                else:
                    # The server ignored our Range header and returned the
                    # entire object (status 200): bound the read itself so a
                    # multi-gigabyte body is never buffered in full, and
                    # this response is terminal regardless of its size --
                    # every retry would re-fetch the identical full body, so
                    # looping would only re-append it pass after pass and
                    # eventually trip a false MAX_HEADER_BYTES overflow.
                    body = _read_bounded(resp, MAX_HEADER_BYTES)
            if not range_honoured:
                return extract_header_line(body, uri.raw)
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
        with (
            _opener.open(uri.raw, timeout=_TIMEOUT_SECONDS) as resp,  # noqa: S310 — scheme gated by parse_csv_uri and _HttpsOnlyRedirectHandler
            open(dest, "wb") as f,
        ):
            shutil.copyfileobj(resp, f)
        return dest


assert issubclass(HttpsCsvSourceReader, CsvSourceReader)
