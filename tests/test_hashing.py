"""The three hash parts of a node, computed from its ODCS contract through continuo-contracts."""
import dataclasses
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from continuo_contracts.hashing import fold3, shared_hash
from continuo_contracts.python import PythonNode, code_projection, projection_hash

from continuo_python_runtime.errors import ContractError
from continuo_python_runtime.hash_inputs import gather_hash_inputs
from continuo_python_runtime.hashing import (
    config_hash, content_hash_fold, hash_parts, shared_code_hash, source_hash,
)
from tests.conftest import DEMO_SCRIPTS
from tests.odcs_builder import contract_node

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
# The config projection of the py_daily_kpis contract.
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
# The config projection of the demo_orders_csv contract.
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
    return contract_node(**raw)


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
def test_code_projection_and_config_hash_match_the_expected_projection(raw, projection):
    node = _node(raw)
    assert _json(code_projection(node.python)) == projection
    assert config_hash(node) == _jcs_sha256(projection)


@pytest.mark.parametrize(
    ("field", "value"),
    [("owner", "team-b"), ("description", "other words"), ("criticality", "CORE"),
     ("schedule", "hourly"), ("timeout_minutes", 30)],
)
def test_contract_only_fields_leave_the_config_hash_alone(field, value):
    assert config_hash(_node({**PY_DAILY_KPIS, field: value})) == config_hash(_node(PY_DAILY_KPIS))


def test_type_spellings_with_one_meaning_hash_alike():
    respelt = {**PY_DAILY_KPIS, "output_columns": [
        {"name": "day", "type": "date", "nullable": False},
        {"name": "tx_count", "type": "INT", "nullable": False},
        {"name": "total_amount", "type": "DECIMAL(12, 2)", "nullable": False},
    ]}
    assert config_hash(_node(respelt)) == config_hash(_node(PY_DAILY_KPIS))


def _with_read(sql):
    return _node({**PY_DAILY_KPIS, "reads": {"daily": sql}})


def test_read_sql_is_hashed_as_written_except_line_endings_and_outer_whitespace():
    base = _with_read("select a\nfrom t where s = 'ok  x'")
    assert config_hash(_with_read("  select a\r\nfrom t where s = 'ok  x'\n")) == config_hash(base)
    assert config_hash(_with_read("select a\nfrom t where s = 'ok x'")) != config_hash(base)
    assert config_hash(_with_read("SELECT a\nfrom t where s = 'ok  x'")) != config_hash(base)


def test_mode_type_and_primary_key_are_in_the_projection():
    replace = _json(code_projection(_node(FX).python))
    upsert_node = _node({**FX, "mode_type": "upsert", "primary_key": ["rate_date", "base"]})
    upsert = _json(code_projection(upsert_node.python))
    assert (replace["mode_type"], replace["primary_key"]) == ("replace", [])
    assert (upsert["mode_type"], upsert["primary_key"]) == ("upsert", ["rate_date", "base"])
    reordered = _node({**FX, "mode_type": "upsert", "primary_key": ["base", "rate_date"]})
    assert config_hash(reordered) != config_hash(upsert_node)


def test_the_fold_is_the_three_part_fold_older_topology_controllers_check():
    assert content_hash_fold("aaa", "bbb", "ccc") == "sha256:" + hashlib.sha256(b"aaa|bbb|ccc").hexdigest()
    assert content_hash_fold("aaa", "", "ccc") == "sha256:" + hashlib.sha256(b"aaa||ccc").hexdigest()


def test_source_hash_is_bare_hex_of_the_bytes():
    assert source_hash(b"x") == hashlib.sha256(b"x").hexdigest()


def test_shared_code_hash_of_no_units_is_empty():
    assert shared_code_hash({}) == ""


def test_shared_code_hash_sees_two_files_swap_contents():
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


# Config values canonical JSON cannot write. No contract file holds one (yaml_strict refuses
# each), so the test gives them to a loaded node; continuo-contracts refuses to hash them, and
# the runtime reports the refusal as its own ContractError.
UNHASHABLE_CONFIGS = {
    "integer-beyond-2^53-1": {"n": 2**53},
    "nan": {"n": float("nan")},
    "infinity": {"n": float("inf")},
    "nesting-beyond-99-levels": _nested(120),
    "lone-surrogate": {"k": "\ud800"},
}


@pytest.mark.parametrize("config", UNHASHABLE_CONFIGS.values(), ids=UNHASHABLE_CONFIGS.keys())
def test_a_config_the_library_refuses_is_a_contract_error(config):
    node = _node(PY_DAILY_KPIS)
    node = dataclasses.replace(node, python=dataclasses.replace(node.python, config=config))
    with pytest.raises(ContractError, match=r"analytics\.py_daily_kpis"):
        config_hash(node)


# What runtime 0.12.0 wrote for the demo's three nodes, from the files in tests/fixtures/demo/
# service-py/old and the scripts of `demo_repo`.
RELEASED_0_12_0 = {
    "py_daily_kpis": {
        "source_hash": "1cc92fd7c7a44a9f918c7377bb51f702666c797ffb71a3e06790ce0ac429f6e4",
        "shared_code_hash": "e0fd76e02996a0abc2213ce8af6866e5b216cb025d290c6a1e0936f7669025c7",
        "config_hash": "a01d7d6065f055bb2d87959aaf178d08f5a3f96c86912099b9876239d84f6efe",
        "content_hash": "sha256:f193d76010cf42b96d141a81bb069dac86465335fc92d895e13be0cc600ffbdc",
    },
    "demo_orders_csv": {
        "source_hash": "0769087054c3ec385be6498360e294567e40c36fbd8f7d05894cd08933ea6de0",
        "shared_code_hash": "",
        "config_hash": "d1c0c0ea3f0f6833dcfb724c85a8d77145c0ec96ec7ec861e5aefda5803fbe5a",
        "content_hash": "sha256:2bd901d10d9545921842f2440a0fdfc35cffe7196df7a84c7388106e45f498a3",
    },
    "fx_eur_usd_rates": {
        "source_hash": "9b748fff53e93db6bb6789726703c5b83b310cc61c6e1f5bae5baa0948b6f2e8",
        "shared_code_hash": "",
        "config_hash": "a7b1cb2a467910e53d8685c63634677d52cdb97e0dbd0d4c2990dd96c88f7664",
        "content_hash": "sha256:727b2187840f9ea11ae126f13d852701d7b0ca7333f01005bdd6eb5263061bae",
    },
}
# The in-repo files each demo script imports, the units of its shared_code_hash.
DEMO_UNITS = {
    "py_daily_kpis": {"scripts/kpi_math.py": DEMO_SCRIPTS["scripts/kpi_math.py"]},
    "demo_orders_csv": {},
    "fx_eur_usd_rates": {},
}


def old_hash_parts(entry: Mapping[str, Any], repo_root: Path, units: Mapping[str, bytes]) -> dict[str, str]:
    """The hash parts runtime 0.12.0 wrote for the `nodes:` entry `entry`.

    The three parts come from continuo-contracts and the entry alone, with no contract file read:
    the entry is the library's `PythonNode` as it stands (the keys the library does not know are
    ignored), the source is the bytes of the script, or the uri of a csv node, and `units` are the
    files its script imports.
    """
    node = PythonNode.from_mapping(entry)
    primary = node.reads["csv"].encode() if node.kind == "python-csv" else (repo_root / node.script).read_bytes()
    source, shared, config = hashlib.sha256(primary).hexdigest(), shared_hash(units), projection_hash(node)
    return {
        "source_hash": source,
        "shared_code_hash": shared,
        "config_hash": config,
        "content_hash": fold3(source, shared, config),
    }


def gather(demo_repo, table):
    """The bytes `hash_parts` is given for a demo node: its primary bytes and its units."""
    inputs = gather_hash_inputs(demo_repo.node(table), demo_repo.root)
    return inputs.primary, inputs.units


@pytest.mark.parametrize("table", ["py_daily_kpis", "demo_orders_csv", "fx_eur_usd_rates"])
def test_the_demo_hash_parts_do_not_move(table, demo_repo):
    old = old_hash_parts(demo_repo.old_entry(table), demo_repo.root, DEMO_UNITS[table])
    new = hash_parts(demo_repo.node(table), *gather(demo_repo, table))
    assert new == old
    assert new == RELEASED_0_12_0[table]
