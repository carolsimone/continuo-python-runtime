"""Content hashing: three independent parts folded into one content_hash.

The three parts (``source_hash``, ``shared_code_hash``, ``config_hash``) are
computed independently from already-gathered bytes/dicts and folded together
with ``content_hash_fold``. This module is pure computation — no filesystem
access, no knowledge of ``closure.py`` — so the known-vector tests can call
``content_hash_fold`` on bare strings with no entry or file involved.

The fold formula and the ``shared_code_hash`` sorted-digest fold are wire
contracts mirrored byte-for-byte from continuo's manifest-controller
(``content_hash_fold``) and its dbt-side shared-code hashing. Every byte here
is load-bearing: manifest-controller independently recomputes the fold from
the parts this repo emits and rejects the release artifact if the two
results differ.
"""

from collections.abc import Iterable
from copy import deepcopy
from hashlib import sha256
from json import dumps

HASH_PART_FIELDS = ("source_hash", "shared_code_hash", "config_hash")
CONTENT_HASH_FIELD = "content_hash"


def canonical_entry(entry: dict) -> dict:
    """Deep-copy *entry*, drop the four hash fields, whitespace-normalize reads.

    Drops ``content_hash`` and all three of ``HASH_PART_FIELDS`` from the
    basis, and whitespace-normalizes every ``str`` value in ``reads`` via
    ``" ".join(sql.split())``. Everything else is preserved verbatim,
    including ``config``.
    """
    canonical = deepcopy(entry)

    canonical.pop(CONTENT_HASH_FIELD, None)
    for field in HASH_PART_FIELDS:
        canonical.pop(field, None)

    reads = canonical.get("reads")
    if isinstance(reads, dict):
        for key, value in reads.items():
            if isinstance(value, str):
                reads[key] = " ".join(value.split())

    return canonical


def canonical_json(entry: dict) -> str:
    """`json.dumps(canonical_entry(entry), sort_keys=True, separators=(",", ":"))`."""
    return dumps(canonical_entry(entry), sort_keys=True, separators=(",", ":"))


def source_hash(script_bytes: bytes) -> str:
    """Bare hex `sha256(script_bytes)` — the node's own script, byte-for-byte."""
    return sha256(script_bytes).hexdigest()


def shared_code_hash(member_bytes: Iterable[bytes]) -> str:
    """`""` when the closure is empty, else bare hex of the sorted-digest fold."""
    unit_hashes = sorted(sha256(member).hexdigest() for member in member_bytes)
    if not unit_hashes:
        return ""
    return sha256("".join(unit_hashes).encode()).hexdigest()


def config_hash(entry: dict) -> str:
    """Bare hex `sha256(canonical_json(entry).encode())`."""
    return sha256(canonical_json(entry).encode()).hexdigest()


def content_hash_fold(source: str, shared: str, config: str) -> str:
    """`"sha256:" + sha256(f"{source}|{shared}|{config}".encode()).hexdigest()`."""
    return "sha256:" + sha256(f"{source}|{shared}|{config}".encode()).hexdigest()


def hash_parts(
    entry: dict, script_bytes: bytes, member_bytes: Iterable[bytes]
) -> dict[str, str]:
    """Return all four fields: the three parts plus their fold."""
    s = source_hash(script_bytes)
    sh = shared_code_hash(member_bytes)
    c = config_hash(entry)
    return {
        "source_hash": s,
        "shared_code_hash": sh,
        "config_hash": c,
        "content_hash": content_hash_fold(s, sh, c),
    }
