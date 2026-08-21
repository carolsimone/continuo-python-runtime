"""continuo_python_runtime/csv_readers/__init__.py"""
from continuo_python_runtime.csv_source import CsvSourceReader, CsvUri
from continuo_python_runtime.csv_readers.https import HttpsCsvSourceReader
from continuo_python_runtime.csv_readers.s3 import S3CsvSourceReader


def reader_for(uri: CsvUri) -> CsvSourceReader:
    """Composition edge: pick the adapter for the parsed scheme.

    parse_csv_uri already constrains uri.scheme to "s3" or "https", but the
    dispatch stays explicit (rather than an s3/else fallback) so a scheme
    added to the parser without a matching adapter fails loudly here too."""
    if uri.scheme == "s3":
        return S3CsvSourceReader()
    if uri.scheme == "https":
        return HttpsCsvSourceReader()
    raise ValueError(f"unsupported csv scheme: {uri.scheme!r}")
