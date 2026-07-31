from continuo_python_runtime.hashing import canonical_entry, content_hash

ENTRY = {
    "schema": "analytics", "table": "t", "owner": "m", "schedule": "daily",
    "criticality": "SECONDARY", "script": "scripts/t.py", "description": "",
    "reads": {"ids": "select id\n  from   analytics.a"},
    "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
}
SCRIPT = b"def run(ctx):\n    return ctx.read('ids')\n"


def test_canonical_entry_drops_content_hash():
    with_hash = {**ENTRY, "content_hash": "sha256:stale"}
    canonical = canonical_entry(with_hash)
    assert "content_hash" not in canonical


def test_canonical_entry_whitespace_normalizes_reads():
    entry_with_whitespace = {
        **ENTRY,
        "reads": {"ids": "select id\n  from   analytics.a"}
    }
    canonical = canonical_entry(entry_with_whitespace)
    assert canonical["reads"]["ids"] == "select id from analytics.a"


def test_sql_whitespace_insensitive():
    reformatted = {**ENTRY, "reads": {"ids": "select    id from analytics.a"}}
    assert content_hash(ENTRY, SCRIPT) == content_hash(reformatted, SCRIPT)


def test_key_order_insensitive():
    reordered = dict(reversed(list(ENTRY.items())))
    assert content_hash(ENTRY, SCRIPT) == content_hash(reordered, SCRIPT)


def test_existing_content_hash_field_excluded():
    assert content_hash({**ENTRY, "content_hash": "sha256:stale"}, SCRIPT) == content_hash(ENTRY, SCRIPT)


def test_semantic_edits_change_hash():
    assert content_hash({**ENTRY, "reads": {"ids": "select id2 from analytics.a"}}, SCRIPT) != content_hash(ENTRY, SCRIPT)
    assert content_hash({**ENTRY, "reads": {"renamed": ENTRY["reads"]["ids"]}}, SCRIPT) != content_hash(ENTRY, SCRIPT)
    assert content_hash({**ENTRY, "owner": "other"}, SCRIPT) != content_hash(ENTRY, SCRIPT)


def test_script_is_byte_sensitive_including_whitespace():
    assert content_hash(ENTRY, SCRIPT + b" ") != content_hash(ENTRY, SCRIPT)


def test_prefix_format():
    h = content_hash(ENTRY, SCRIPT)
    assert h.startswith("sha256:") and len(h) == 7 + 64
