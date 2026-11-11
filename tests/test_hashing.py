"""The three hash parts, computed through continuo-contracts (design 4.2, N1 to N4)."""
import hashlib
import json

import pytest
from continuo_contracts.python import code_projection

from continuo_python_runtime.contract.loader import parse_node
from continuo_python_runtime.errors import ContractError
from continuo_python_runtime.hashing import (
    config_hash, content_hash_fold, hash_parts, shared_code_hash, source_hash, to_python_node,
)

PY_DAILY_KPIS = {
    "schema": "analytics", "table": "py_daily_kpis", "owner": "service-py", "schedule": "daily",
    "criticality": "SECONDARY", "script": "scripts/py_daily_kpis.py",
    "description": "Daily transaction count and total amount in EUR, rolled up from analytics.daily_transactions.",
    "reads": {"daily": "select created_at, amount_eur from analytics.daily_transactions"},
    "output_columns": [
        {"name": "day", "type": "DATE", "nullable": False},
        {"name": "tx_count", "type": "INTEGER", "nullable": False},
        {"name": "total_amount", "type": "NUMERIC(12,2)", "nullable": False},
    ],
}
# The design's projection of py_daily_kpis (section 3.6).
PY_DAILY_KPIS_PROJECTION = {
    "assets": [], "config": {}, "extra_columns": "raise", "kind": "python-node", "mode_type": "replace",
    "output_columns": [
        {"name": "day", "nullable": False, "type": "DATE"},
        {"name": "tx_count", "nullable": False, "type": "INTEGER"},
        {"name": "total_amount", "nullable": False, "type": "NUMERIC(12,2)"},
    ],
    "primary_key": [],
    "reads": {"daily": "select created_at, amount_eur from analytics.daily_transactions"},
    "schema": "analytics", "script": "scripts/py_daily_kpis.py", "secret_ref": "", "table": "py_daily_kpis",
}
DEMO_ORDERS_CSV = {
    "schema": "analytics", "table": "demo_orders_csv", "owner": "service-py", "schedule": "daily",
    "criticality": "SECONDARY", "kind": "python-csv",
    "reads": {"csv": "s3://continuo-dev/static-files/demo/orders.csv"},
    "output_columns": [
        {"name": "order_id", "type": "INTEGER", "nullable": False},
        {"name": "customer", "type": "VARCHAR(64)", "nullable": True},
        {"name": "amount", "type": "DOUBLE PRECISION", "nullable": True},
    ],
}
# The design's projection of demo_orders_csv (section 3.7).
DEMO_ORDERS_CSV_PROJECTION = {
    "assets": [], "config": {}, "extra_columns": "raise", "kind": "python-csv", "mode_type": "replace",
    "output_columns": [
        {"name": "order_id", "nullable": False, "type": "INTEGER"},
        {"name": "customer", "nullable": True, "type": "VARCHAR(64)"},
        {"name": "amount", "nullable": True, "type": "DOUBLE PRECISION"},
    ],
    "primary_key": [], "reads": {"csv": "s3://continuo-dev/static-files/demo/orders.csv"},
    "schema": "analytics", "script": "", "secret_ref": "", "table": "demo_orders_csv",
}
FX = {
    "schema": "fixture", "table": "fx", "owner": "o", "schedule": "daily", "criticality": "SECONDARY",
    "kind": "python-api", "script": "scripts/fx.py",
    "output_columns": [
        {"name": "rate_date", "type": "DATE", "nullable": False},
        {"name": "base", "type": "VARCHAR(3)", "nullable": False},
        {"name": "rate", "type": "DECIMAL(12,6)", "nullable": True},
    ],
}


def _node(raw):
    return parse_node(raw, "f.yml")


def _json(value):
    return json.loads(json.dumps(value))


def _jcs_sha256(value) -> str:
    """For ASCII values with no floats, RFC 8785 bytes equal sorted compact json.dumps."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


@pytest.mark.parametrize(
    ("raw", "projection"),
    [(PY_DAILY_KPIS, PY_DAILY_KPIS_PROJECTION), (DEMO_ORDERS_CSV, DEMO_ORDERS_CSV_PROJECTION)],
    ids=["python-node", "python-csv"],
)
def test_code_projection_and_config_hash_match_the_design(raw, projection):
    node = _node(raw)
    assert _json(code_projection(to_python_node(node))) == projection
    assert config_hash(node) == _jcs_sha256(projection)


@pytest.mark.parametrize(
    ("field", "value"),
    [("owner", "team-b"), ("description", "other words"), ("criticality", "CORE"),
     ("schedule", "hourly"), ("timeout_minutes", 30)],
)
def test_contract_only_fields_leave_the_config_hash_alone(field, value):  # C19
    assert config_hash(_node({**PY_DAILY_KPIS, field: value})) == config_hash(_node(PY_DAILY_KPIS))


def test_type_spellings_with_one_meaning_hash_alike():  # C20
    respelt = {**PY_DAILY_KPIS, "output_columns": [
        {"name": "day", "type": "date", "nullable": False},
        {"name": "tx_count", "type": "INT", "nullable": False},
        {"name": "total_amount", "type": "DECIMAL(12, 2)", "nullable": False},
    ]}
    assert config_hash(_node(respelt)) == config_hash(_node(PY_DAILY_KPIS))


def _with_read(sql):
    return _node({**PY_DAILY_KPIS, "reads": {"daily": sql}})


def test_read_sql_is_hashed_as_written_except_line_endings_and_outer_whitespace():  # C14, N2
    base = _with_read("select a\nfrom t where s = 'ok  x'")
    assert config_hash(_with_read("  select a\r\nfrom t where s = 'ok  x'\n")) == config_hash(base)
    assert config_hash(_with_read("select a\nfrom t where s = 'ok x'")) != config_hash(base)
    assert config_hash(_with_read("SELECT a\nfrom t where s = 'ok  x'")) != config_hash(base)


def test_mode_type_and_primary_key_are_in_the_projection():
    replace = _json(code_projection(to_python_node(_node(FX))))
    upsert_node = _node({**FX, "mode_type": "upsert", "primary_key": ["rate_date", "base"]})
    upsert = _json(code_projection(to_python_node(upsert_node)))
    assert (replace["mode_type"], replace["primary_key"]) == ("replace", [])
    assert (upsert["mode_type"], upsert["primary_key"]) == ("upsert", ["rate_date", "base"])
    reordered = _node({**FX, "mode_type": "upsert", "primary_key": ["base", "rate_date"]})
    assert config_hash(reordered) != config_hash(upsert_node)


def test_the_fold_is_the_three_part_fold_older_topology_controllers_check():  # N1
    assert content_hash_fold("aaa", "bbb", "ccc") == "sha256:" + hashlib.sha256(b"aaa|bbb|ccc").hexdigest()
    assert content_hash_fold("aaa", "", "ccc") == "sha256:" + hashlib.sha256(b"aaa||ccc").hexdigest()


def test_source_hash_is_bare_hex_of_the_bytes():
    assert source_hash(b"x") == hashlib.sha256(b"x").hexdigest()


def test_shared_code_hash_of_no_units_is_empty():
    assert shared_code_hash({}) == ""


def test_shared_code_hash_sees_two_files_swap_contents():  # C13
    assert shared_code_hash({"lib/p.py": b"V = 7\n", "lib/q.py": b"V = -7\n"}) != shared_code_hash(
        {"lib/p.py": b"V = -7\n", "lib/q.py": b"V = 7\n"}
    )


def test_shared_code_hash_sees_a_moved_file():
    assert shared_code_hash({"lib/p.py": b"x"}) != shared_code_hash({"lib/r.py": b"x"})


def test_shared_code_hash_ignores_mapping_order():
    assert shared_code_hash({"a.py": b"1", "b.py": b"2"}) == shared_code_hash({"b.py": b"2", "a.py": b"1"})


def test_hash_parts_folds_its_own_parts():
    node = _node(PY_DAILY_KPIS)
    parts = hash_parts(node, b"def run(ctx): ...\n", {"lib/a.py": b"A = 1\n"})
    assert parts["source_hash"] == source_hash(b"def run(ctx): ...\n")
    assert parts["shared_code_hash"] == shared_code_hash({"lib/a.py": b"A = 1\n"})
    assert parts["config_hash"] == config_hash(node)
    assert parts["content_hash"] == content_hash_fold(
        parts["source_hash"], parts["shared_code_hash"], parts["config_hash"]
    )


def _nested(levels: int) -> dict:
    config: dict = {}
    cursor = config
    for _ in range(levels - 1):
        cursor["k"] = {}
        cursor = cursor["k"]
    return config


# The loader accepts these config values; continuo-contracts refuses them, and the runtime
# reports the refusal as its own ContractError.
UNHASHABLE_CONFIGS = {
    "integer-beyond-2^53-1": {"n": 2**53},
    "nan": {"n": float("nan")},
    "infinity": {"n": float("inf")},
    "nesting-beyond-99-levels": _nested(120),
    "lone-surrogate": {"k": "\ud800"},
}


@pytest.mark.parametrize("config", UNHASHABLE_CONFIGS.values(), ids=UNHASHABLE_CONFIGS.keys())
def test_a_config_the_library_refuses_is_a_contract_error(config):
    node = _node({**PY_DAILY_KPIS, "config": config})
    with pytest.raises(ContractError, match=r"analytics\.py_daily_kpis"):
        config_hash(node)
