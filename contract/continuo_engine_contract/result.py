"""Cross-language contract for one validation node's structured result.

validation_runner.py prints exactly one sentinel-framed block on stdout as its LAST
output. The k8s-controller extracts the block from the pod log, uploads the JSON to
S3, and surfaces it as run_results_uri. The remediation classifier reads status +
message deterministically, falling back to the text log when the block is absent.

status uses dbt's RunStatus vocabulary (success | error | fail | skipped).
"""
import json
from collections.abc import Mapping
from typing import Any

SENTINEL_BEGIN = "===CONTINUO_VALIDATION_RESULT_BEGIN==="
SENTINEL_END = "===CONTINUO_VALIDATION_RESULT_END==="

SCHEMA_VERSION = 1

# The keys ``result_block`` writes itself; a caller's ``fields`` may not reuse them.
_RESERVED_KEYS = frozenset(
    {"schema_version", "status", "message", "failures", "unique_id", "reason"}
)


def result_block(
    status: str,
    message: str = "",
    failures: int = 0,
    unique_id: str = "",
    *,
    reason: str = "",
    fields: Mapping[str, Any] | None = None,
) -> str:
    """Return the sentinel-framed, single-line-JSON block for one node.

    The JSON is compact (no embedded newlines) so the consumer can split the
    block out of a multi-line pod log by marker lines alone. ``reason``, a
    machine-readable failure code, is written after ``unique_id`` and only when
    non-empty, so a block without one keeps the five standard keys byte for byte.
    ``fields`` carries an op's own keys; they follow ``reason`` in the order
    given, and a key that is one of the standard keys or ``reason`` raises
    ``ValueError`` naming it.
    """
    extra = dict(fields or {})
    for key in extra:
        if key in _RESERVED_KEYS:
            raise ValueError(f"result field {key!r} shadows a standard result key")
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "message": message or "",
        "failures": int(failures or 0),
        "unique_id": unique_id or "",
    }
    if reason:
        doc["reason"] = reason
    doc.update(extra)
    body = json.dumps(doc, separators=(",", ":"))
    return f"{SENTINEL_BEGIN}\n{body}\n{SENTINEL_END}"
