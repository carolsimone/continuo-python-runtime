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
from continuo_python_runtime.types import arrow_type, parse_sql_type

logger = logging.getLogger("continuo_python_runtime.csv_loader")


def produce_csv(node: Node, reader: CsvSourceReader | None = None) -> "pyarrow.Table":
    """Fetch node.reads['csv'] and parse it (RFC4180 defaults) into a Table.

    The caller conforms the result to output_columns exactly as for a script
    node, so declared types — not csv inference — decide the warehouse schema.

    ``output_columns`` names/types are also passed to ``read_csv`` itself as
    its convert schema (via ``ConvertOptions.column_types``): pyarrow's default
    type inference is otherwise the *first* place a value gets interpreted,
    and it can destroy the very lexical value ``conform()`` is supposed to
    preserve -- a VARCHAR column holding ``00123`` infers as int64 and
    conform() writes back ``"123"``, and a NUMERIC column holding a valid
    decimal like ``10.50`` infers as float64, which conform()'s own
    lossy-cast guard then rejects outright. Reading every declared column
    directly as its target Arrow type sidesteps both: the value is parsed
    once, as the type it is actually declared to be.
    """
    uri = parse_csv_uri(node.reads["csv"])
    active_reader = reader if reader is not None else reader_for(uri)
    column_types = {
        col.name: arrow_type(parse_sql_type(col.type)) for col in node.output_columns
    }
    convert_options = pyarrow.csv.ConvertOptions(column_types=column_types)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dest = active_reader.fetch(uri, Path(tmp) / "source.csv")
            table = pyarrow.csv.read_csv(dest, convert_options=convert_options)
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
