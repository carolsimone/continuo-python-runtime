import copy
import hashlib

from continuo_python_runtime.hashing import (
    canonical_entry,
    config_hash,
    content_hash_fold,
    hash_parts,
    shared_code_hash,
    source_hash,
)

ENTRY = {
    "schema": "analytics", "table": "t", "owner": "m", "schedule": "daily",
    "criticality": "SECONDARY", "script": "scripts/t.py", "description": "",
    "reads": {"ids": "select id\n  from   analytics.a"},
    "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
}
SCRIPT = b"def run(ctx):\n    return ctx.read('ids')\n"
MEMBER_A = b"def helper_a():\n    return 1\n"
MEMBER_B = b"def helper_b():\n    return 2\n"


# --- Known vectors, pinned to continuo/manifest-controller's fold ---

def test_content_hash_fold_known_vector_full():
    assert content_hash_fold("aaa", "bbb", "ccc") == "sha256:" + hashlib.sha256(b"aaa|bbb|ccc").hexdigest()


def test_content_hash_fold_known_vector_empty_shared():
    assert content_hash_fold("aaa", "", "ccc") == "sha256:" + hashlib.sha256(b"aaa||ccc").hexdigest()


def test_content_hash_fold_flips_on_any_part_change():
    base = content_hash_fold("aaa", "bbb", "ccc")
    assert content_hash_fold("xxx", "bbb", "ccc") != base
    assert content_hash_fold("aaa", "xxx", "ccc") != base
    assert content_hash_fold("aaa", "bbb", "xxx") != base


# --- source_hash ---

def test_source_hash_is_bare_hex_no_prefix():
    h = source_hash(b"x")
    assert h == hashlib.sha256(b"x").hexdigest()
    assert not h.startswith("sha256:")


# --- shared_code_hash ---

def test_shared_code_hash_empty_is_empty_string():
    assert shared_code_hash([]) == ""


def test_shared_code_hash_order_insensitive():
    assert shared_code_hash([MEMBER_A, MEMBER_B]) == shared_code_hash([MEMBER_B, MEMBER_A])


def test_shared_code_hash_sensitive_to_one_byte_change():
    changed = MEMBER_A[:-2] + b"2\n"
    assert changed != MEMBER_A
    assert shared_code_hash([MEMBER_A]) != shared_code_hash([changed])


# --- config_hash ---

def test_config_hash_whitespace_insensitive_in_reads():
    reformatted = {**ENTRY, "reads": {"ids": "select    id from analytics.a"}}
    assert config_hash(ENTRY) == config_hash(reformatted)


def test_config_hash_key_order_insensitive():
    reordered = dict(reversed(list(ENTRY.items())))
    assert config_hash(ENTRY) == config_hash(reordered)


def test_config_hash_sensitive_to_config_edit():
    without_indexes = {**ENTRY, "config": {}}
    with_indexes = {**ENTRY, "config": {"indexes": [{"columns": ["id"]}]}}
    assert config_hash(without_indexes) != config_hash(with_indexes)


def test_config_hash_ignores_stale_content_hash_and_part_fields():
    stale = {
        **ENTRY,
        "content_hash": "sha256:stale",
        "source_hash": "deadbeef",
        "shared_code_hash": "deadbeef",
        "config_hash": "deadbeef",
    }
    assert config_hash(stale) == config_hash(ENTRY)


# --- canonical_entry ---

def test_canonical_entry_does_not_mutate_argument():
    original = copy.deepcopy(ENTRY)
    canonical_entry(ENTRY)
    assert ENTRY == original


def test_canonical_entry_drops_all_four_hash_fields():
    with_hashes = {
        **ENTRY,
        "content_hash": "sha256:stale",
        "source_hash": "deadbeef",
        "shared_code_hash": "deadbeef",
        "config_hash": "deadbeef",
    }
    canonical = canonical_entry(with_hashes)
    assert "content_hash" not in canonical
    assert "source_hash" not in canonical
    assert "shared_code_hash" not in canonical
    assert "config_hash" not in canonical


def test_canonical_entry_whitespace_normalizes_reads():
    canonical = canonical_entry(ENTRY)
    assert canonical["reads"]["ids"] == "select id from analytics.a"


def test_canonical_entry_preserves_config_verbatim():
    entry_with_config = {**ENTRY, "config": {"indexes": [{"columns": ["id"]}]}}
    canonical = canonical_entry(entry_with_config)
    assert canonical["config"] == {"indexes": [{"columns": ["id"]}]}


# --- hash_parts ---

def test_hash_parts_returns_four_keys_and_consistent_fold():
    parts = hash_parts(ENTRY, SCRIPT, [MEMBER_A, MEMBER_B])
    assert set(parts) == {"source_hash", "shared_code_hash", "config_hash", "content_hash"}
    assert parts["content_hash"] == content_hash_fold(
        parts["source_hash"], parts["shared_code_hash"], parts["config_hash"]
    )
