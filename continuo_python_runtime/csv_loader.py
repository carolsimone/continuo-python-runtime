"""continuo_python_runtime/csv_loader.py

Producer for python-csv nodes: materialize the declared table from the csv
source alone. Everything from conform() down (type coercion, extra_columns
policy, ensure_table, transactional load) is the existing harness path —
this module only turns the contract entry into a pyarrow Table.
"""
import logging
import tempfile
from pathlib import Path

import pyarrow.csv  # type: ignore[import-untyped]

from continuo_python_runtime.contract.model import Node
from continuo_python_runtime.csv_readers import reader_for
from continuo_python_runtime.csv_source import CsvSourceReader, parse_csv_uri
from continuo_python_runtime.errors import LoadError

logger = logging.getLogger("continuo_python_runtime.csv_loader")


def produce_csv(node: Node, reader: CsvSourceReader | None = None) -> "pyarrow.Table":
    """Fetch node.reads['csv'] and parse it (RFC4180 defaults) into a Table.

    The caller conforms the result to output_columns exactly as for a script
    node, so declared types — not csv inference — decide the warehouse schema.
    """
    uri = parse_csv_uri(node.reads["csv"])
    active_reader = reader if reader is not None else reader_for(uri)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dest = active_reader.fetch(uri, Path(tmp) / "source.csv")
            table = pyarrow.csv.read_csv(dest)
    except LoadError:
        raise
    except Exception as exc:
        raise LoadError(f"csv fetch failed for {node.relation}: {exc}") from exc
    declared = {col.name for col in node.output_columns}
    extras = set(table.column_names) - declared
    if extras:
        # Spec parity with the validation runner's csv_source header check
        # (continuo_python_runtime/validation/runner.py): extra_columns: drop
        # silently discards these at conform() time, so this structured
        # warning is the only place the RUN path surfaces which columns were
        # dropped.
        logger.warning(
            "csv_header_extra_columns node=%s columns=%s — columns present in the "
            "csv but not declared in output_columns; they will not be loaded",
            node.relation, sorted(extras))
    logger.info("csv source %s: %d rows, columns=%s",
                uri.raw, table.num_rows, table.column_names)
    return table
