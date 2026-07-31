import copy
import hashlib
import json


def canonical_entry(entry: dict) -> dict:
    """
    Deep-copy the entry, drop content_hash field, and whitespace-normalize
    every value in reads via " ".join(sql.split()).
    """
    canonical = copy.deepcopy(entry)

    # Drop content_hash if present
    canonical.pop("content_hash", None)

    # Whitespace-normalize every value in reads
    if "reads" in canonical and isinstance(canonical["reads"], dict):
        for key, value in canonical["reads"].items():
            if isinstance(value, str):
                canonical["reads"][key] = " ".join(value.split())

    return canonical


def content_hash(entry: dict, script_bytes: bytes) -> str:
    """
    Compute content hash using the formula:
    "sha256:" + sha256(json.dumps(canonical_entry(entry), sort_keys=True, separators=(",", ":")).encode() + b"\x00" + script_bytes).hexdigest()
    """
    canonical = canonical_entry(entry)
    json_str = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    data = json_str.encode() + b"\x00" + script_bytes
    hash_digest = hashlib.sha256(data).hexdigest()
    return "sha256:" + hash_digest
