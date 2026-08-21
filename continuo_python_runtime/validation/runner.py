"""Build a single node as an empty table in the candidate schema (blue/green validation).

Dispatches on ``VALIDATION_OP`` env var (default ``build_from_sql``):
- ``build_from_sql``: fetch the node's compiled SQL from S3 (``CANDIDATE_SQL_URI``) and
  materialize it empty (models/snapshots).
- ``clone_from_prod``: clone an existing prod table's shape empty from ``PROD_SCHEMA``
  (unchanged upstreams, including seeds).
- ``build_from_columns``: for python nodes, which have no SELECT to shape their output
  from. Fetch the node's validation spec JSON from S3 (``CANDIDATE_SPEC_URI`` —
  ``{"reads": [sql, ...], "output_columns": [{"name","type","nullable"}, ...],
  "config": {...}, "csv_source": "s3://..." | "https://..."}``; ``config`` and
  ``csv_source`` are both optional. When ``csv_source`` is set, its header row is
  fetched (without downloading the full object) and checked against the declared
  output columns: a declared column missing from the header fails the release gate,
  while a header column not declared only logs a ``csv_header_extra_columns``
  warning (it is silently dropped at load time). ``config`` defaults to ``{}``.
  Every declared read is bind-checked against the candidate schema so an upstream
  that dropped a column the script reads fails the release gate, then the output
  table is materialized empty from the declared typed columns and the declared
  physical layout.

The engine adapter is discovered from the single installed
``continuo_engine.adapters`` entry point — each runner image installs exactly one.
stdout is reserved exclusively for the runner's one structured ``result_block``,
printed as its last line; all diagnostics go to stderr via the ``logging`` module.
A non-zero exit marks the node failed.
"""
import csv
import json
import logging
import os
import sys

from continuo_engine_contract import result  # type: ignore[import-untyped]
from continuo_engine_contract.port import (  # type: ignore[import-untyped]
    AdapterDiscoveryError,
    discover_adapter,
)
from continuo_python_runtime.csv_readers import reader_for
from continuo_python_runtime.csv_source import check_header, parse_csv_uri
from continuo_python_runtime.validation import s3

logger = logging.getLogger("validation_runner")


def _node_id() -> str:
    """Best-known node identity for a result block: ``NODE_ID`` env, else ``""``.

    The single source of NODE_ID resolution shared by every block-emitting path.
    ``main`` layers a ``model.{table}`` fallback on top once TABLE_NAME is known;
    the early ``_require`` failures (which may be the missing TABLE_NAME itself)
    cannot, so they fall back to this bare value.
    """
    return os.environ.get("NODE_ID", "")


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        logger.error("missing required env var %s", name)
        print(
            result.result_block(
                "error", f"missing required env var {name}",
                unique_id=_node_id(),
            ),
            flush=True,
        )
        sys.exit(2)
    return value


def load_candidate_sql() -> str:
    """Fetch this node's candidate SQL from S3 at ``CANDIDATE_SQL_URI``.

    Returns the raw UTF-8 body (no stripping; the caller normalizes). Returns ``""``
    when ``CANDIDATE_SQL_URI`` is unset/empty (nothing to validate). Raises on
    invalid-URI or S3-download errors so ``main`` maps them to a structured block.
    """
    uri = os.environ.get("CANDIDATE_SQL_URI", "")
    if not uri:
        return ""
    bucket, key = s3.parse_s3_uri(uri)
    client = s3.make_s3_client()
    body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    decoded: str = body.decode("utf-8")
    return decoded


def load_candidate_spec() -> dict:
    """Fetch this node's validation spec JSON from S3 at ``CANDIDATE_SPEC_URI``.

    Returns the parsed dict. Raises on unset/empty URI, invalid URI, S3
    download errors, invalid JSON, or JSON that parses to something other than
    an object (e.g. a list, ``null``, a number, or a string) — ``main`` maps
    every failure to a structured block.
    """
    uri = os.environ.get("CANDIDATE_SPEC_URI", "")
    if not uri:
        raise ValueError("CANDIDATE_SPEC_URI is unset or empty")
    bucket, key = s3.parse_s3_uri(uri)
    client = s3.make_s3_client()
    body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    spec = json.loads(body.decode("utf-8"))
    if not isinstance(spec, dict):
        raise ValueError(
            f"candidate spec must be a JSON object, got {type(spec).__name__}"
        )
    return spec


_NODE_OPS = ("build_from_sql", "clone_from_prod", "build_from_columns")
_SCHEMA_OPS = ("ensure_schema", "drop_schema")


def main() -> None:
    """Run one validation op end to end; exits non-zero on failure.

    Node ops (``build_from_sql``/``clone_from_prod``/``build_from_columns``)
    materialize one empty node table and require ``TABLE_NAME``. Schema ops
    (``ensure_schema``/``drop_schema``) act on the whole candidate schema —
    the executor schedules them as one-shot engine-image Jobs to own the
    candidate-schema lifecycle without connecting to the warehouse itself —
    and take no table.
    """
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s"
    )
    schema = _require("DBT_TARGET_SCHEMA")
    op = os.environ.get("VALIDATION_OP", "build_from_sql")

    # Gather op-specific inputs and identity BEFORE touching the adapter, surfacing
    # input errors as a structured block (preserves the prior contract + exit codes).
    table = None
    candidate_sql = None
    prod_schema = None
    spec: dict | None = None
    config: dict = {}
    csv_source: str = ""
    if op in _NODE_OPS:
        table = _require("TABLE_NAME")
        unique_id = _node_id() or f"model.{table}"
        if op == "build_from_sql":
            try:
                raw_sql = load_candidate_sql()
            except Exception as exc:
                uri = os.environ.get("CANDIDATE_SQL_URI", "")
                logger.error("ERROR fetching candidate SQL from %r: %s", uri, exc)
                print(result.result_block("error", str(exc), unique_id=unique_id), flush=True)
                sys.exit(1)
            if not raw_sql:
                logger.error(
                    "CANDIDATE_SQL_URI is unset or the object is empty for a "
                    "build_from_sql node; cannot validate"
                )
                print(result.result_block("error", "CANDIDATE_SQL_URI is unset or empty",
                                          unique_id=unique_id), flush=True)
                sys.exit(2)
            candidate_sql = raw_sql
        elif op == "build_from_columns":
            try:
                spec = load_candidate_spec()
            except (ValueError, json.JSONDecodeError) as exc:
                logger.error("invalid candidate spec: %s", exc)
                print(result.result_block("error", str(exc), unique_id=unique_id), flush=True)
                sys.exit(2)
            except Exception as exc:
                uri = os.environ.get("CANDIDATE_SPEC_URI", "")
                logger.error("ERROR fetching candidate spec from %r: %s", uri, exc)
                print(result.result_block("error", str(exc), unique_id=unique_id), flush=True)
                sys.exit(1)
            if not spec.get("output_columns"):
                logger.error("candidate spec has no output_columns; cannot validate")
                print(result.result_block("error", "candidate spec has no output_columns",
                                          unique_id=unique_id), flush=True)
                sys.exit(2)
            raw_config = spec.get("config")
            if raw_config is not None and not isinstance(raw_config, dict):
                # Engine-blind: the runner only decides that the block is a JSON
                # object. Which keys are legal is the installed adapter's call.
                msg = (
                    f"candidate spec 'config' must be a JSON object, "
                    f"got {type(raw_config).__name__}"
                )
                logger.error("%s", msg)
                print(result.result_block("error", msg, unique_id=unique_id), flush=True)
                sys.exit(2)
            config = raw_config or {}
            csv_source = spec.get("csv_source", "")
            if csv_source and not isinstance(csv_source, str):
                msg = f"candidate spec 'csv_source' must be a string, got {type(csv_source).__name__}"
                logger.error("%s", msg)
                print(result.result_block("error", msg, unique_id=unique_id), flush=True)
                sys.exit(2)
        else:
            prod_schema = _require("PROD_SCHEMA")
    elif op in _SCHEMA_OPS:
        unique_id = _node_id() or f"schema.{schema}"
    else:
        unique_id = _node_id() or f"schema.{schema}"
        logger.error("unknown VALIDATION_OP %r", op)
        print(result.result_block("error", f"unknown VALIDATION_OP {op!r}",
                                  unique_id=unique_id), flush=True)
        sys.exit(2)

    # Engine selection is discovery, not configuration: the image installs one adapter.
    try:
        engine, adapter_cls = discover_adapter()
    except AdapterDiscoveryError as exc:
        logger.error("%s", exc)
        print(result.result_block("error", str(exc), unique_id=unique_id), flush=True)
        sys.exit(2)

    missing = [v for v in adapter_cls.required_env() if not os.environ.get(v)]
    if missing:
        msg = f"missing required env for engine {engine!r}: {', '.join(missing)}"
        logger.error("%s", msg)
        print(result.result_block("error", msg, unique_id=unique_id), flush=True)
        sys.exit(2)

    # close() runs exactly once, in the finally, on every path: the success case,
    # the op error (sys.exit raises SystemExit, which still unwinds finally), and
    # a from_env() failure (adapter stays None). A close() failure only logs — the
    # primary error, if any, is already the SystemExit propagating through.
    adapter = None
    try:
        adapter = adapter_cls.from_env()
        if op == "ensure_schema":
            adapter.ensure_schema(schema)
        elif op == "drop_schema":
            adapter.drop_schema(schema)
        else:
            adapter.ensure_schema(schema)
            assert table is not None, "table must be set for a node op"
            if op == "build_from_sql":
                assert candidate_sql is not None, "candidate_sql must be set for build_from_sql"
                adapter.build_empty_from_sql(schema, table, candidate_sql)
            elif op == "build_from_columns":
                assert spec is not None, "spec must be set for build_from_columns"
                if csv_source:
                    csv_uri = parse_csv_uri(csv_source)
                    header_line = reader_for(csv_uri).fetch_header_line(csv_uri)
                    if not header_line:
                        raise ValueError(
                            "csv source has no header line (empty or unreadable): "
                            f"{csv_source}"
                        )
                    declared = [c["name"] for c in spec["output_columns"]]
                    extras = check_header(next(csv.reader([header_line])), declared)
                    if extras:
                        logger.warning(
                            "csv_header_extra_columns node=%s columns=%s — columns present in the "
                            "csv but not declared in output_columns; they will not be loaded",
                            unique_id, sorted(extras))
                for read_sql in spec.get("reads", []):
                    adapter.check_binds(read_sql)
                adapter.build_empty_from_columns(schema, table, spec["output_columns"], config)
            else:
                assert prod_schema is not None, "prod_schema must be set for clone_from_prod"
                adapter.clone_empty_from_prod(schema, prod_schema, table)
    except Exception as exc:
        logger.error("ERROR op=%s on schema %s: %s", op, schema, exc)
        print(result.result_block("error", str(exc), unique_id=unique_id), flush=True)
        sys.exit(1)
    finally:
        if adapter is not None:
            try:
                adapter.close()
            except Exception as close_exc:  # never mask the primary outcome
                logger.error("adapter close failed: %s", close_exc)

    logger.info("ran op=%s on schema=%s (engine=%s)", op, schema, engine)
    print(result.result_block("success", unique_id=unique_id), flush=True)


if __name__ == "__main__":
    main()
