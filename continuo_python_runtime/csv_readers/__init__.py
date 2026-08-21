"""continuo_python_runtime/csv_readers/__init__.py"""
from continuo_python_runtime.csv_source import CsvSourceReader, CsvUri
from continuo_python_runtime.csv_readers.https import HttpsCsvSourceReader
from continuo_python_runtime.csv_readers.s3 import S3CsvSourceReader


def reader_for(uri: CsvUri) -> CsvSourceReader:
    """Composition edge: pick the adapter for the parsed scheme."""
    if uri.scheme == "s3":
        return S3CsvSourceReader()
    return HttpsCsvSourceReader()
