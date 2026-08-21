"""CSV-source rules shared by the run harness and the validation runner:
the URI grammar, the header-conformance rule, and the reader port both
consumers depend on. Dependency-free — adapters that do I/O live in
continuo_python_runtime/csv_readers/ and implement CsvSourceReader.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

HEADER_PROBE_BYTES = 65_536   # first ranged fetch when probing for the header line
MAX_HEADER_BYTES = 1_048_576  # a header line longer than this is a failure


@dataclass(frozen=True)
class CsvUri:
    scheme: str  # "s3" | "https"
    raw: str
    bucket: str = ""  # s3 only
    key: str = ""     # s3 only


def parse_csv_uri(uri: str) -> CsvUri:
    """Parse a csv source URI. Accepts exactly s3://bucket/key and https://...

    Raises ValueError for anything else (http:// included), and for a
    non-string ``uri`` (e.g. a contract's `reads: {csv: 123}`) rather than
    letting `.startswith()` raise a bare AttributeError/TypeError -- callers
    (the contract loader) catch ValueError to turn this into a ContractError,
    so a malformed uri fails at lint/parse time, never at run time.
    """
    if not isinstance(uri, str):
        raise ValueError(f"csv uri must be a string, got {type(uri).__name__}: {uri!r}")
    if uri.startswith("s3://"):
        bucket, _, key = uri[len("s3://"):].partition("/")
        if not bucket or not key:
            raise ValueError(f"invalid s3 csv uri (missing bucket or key): {uri!r}")
        return CsvUri(scheme="s3", raw=uri, bucket=bucket, key=key)
    if uri.startswith("https://"):
        remainder = uri[len("https://"):]
        host, _, _ = remainder.partition("/")
        if host:
            return CsvUri(scheme="https", raw=uri)
    raise ValueError(
        f"invalid csv uri {uri!r}: must be s3://bucket/key or https://..."
    )


def extract_header_line(data: bytes, source_desc: str) -> str:
    """Return the first line of ``data`` (no trailing newline), decoded as
    utf-8-sig so a UTF-8 byte-order mark on the CSV's first byte does not end
    up prepended to the first column name.

    Shared by every :class:`CsvSourceReader` adapter's ``fetch_header_line``:
    the check must run on the *resolved line itself* (the bytes up to, and
    including the absence of, the first newline), not merely on an
    intermediate buffer length -- a newline that only arrives after the
    accumulated buffer has already grown past ``MAX_HEADER_BYTES`` must still
    be rejected as oversized, not returned as a successful (if enormous)
    header line.

    Raises:
        ValueError: If the line exceeds ``MAX_HEADER_BYTES``.
    """
    line = data.split(b"\n", 1)[0] if b"\n" in data else data
    if len(line) > MAX_HEADER_BYTES:
        raise ValueError(f"csv header line exceeds {MAX_HEADER_BYTES} bytes: {source_desc}")
    return line.rstrip(b"\r").decode("utf-8-sig")


def check_header(header_cols: list[str], declared_cols: list[str]) -> set[str]:
    """Presence-only header conformance: every declared column must appear in
    the CSV header, in any order. Returns the set of header columns NOT
    declared (extras) so callers can surface them as a warning.

    Raises ValueError naming every missing declared column.
    """
    header = set(header_cols)
    missing = [c for c in declared_cols if c not in header]
    if missing:
        raise ValueError(f"csv header missing declared column(s): {sorted(missing)}")
    return header - set(declared_cols)


class CsvSourceReader(ABC):
    """Port for reading a csv source. Implemented by csv_readers adapters;
    consumed by the run harness (full fetch) and the validation runner
    (header line only). The dependency arrow runs adapter -> this port."""

    @abstractmethod
    def fetch_header_line(self, uri: CsvUri) -> str:
        """Return the CSV's first line (no trailing newline). Raises on an
        unreachable source or a header longer than MAX_HEADER_BYTES."""

    @abstractmethod
    def fetch(self, uri: CsvUri, dest: Path) -> Path:
        """Stream the full object to dest and return dest."""
