"""Harness write modes: writer dispatch, capability check and error mapping."""
import json

import yaml

from continuo_engine_contract.port import WarehouseAdapter

from continuo_python_runtime.harness import run_node
from tests.conftest import FakeAppendAdapter, FakeUpsertAdapter, FakeWarehouseAdapter

FX_COLUMNS = [
    {"name": "rate_date", "type": "DATE", "nullable": False},
    {"name": "base", "type": "VARCHAR(3)", "nullable": False},
    {"name": "rate", "type": "DECIMAL(12,6)", "nullable": True},
]


def _script(dates: str, bases: str, prelude: str = "") -> str:
    return (
        "import datetime\n"
        "import pathlib\n"
        "from decimal import Decimal\n\n"
        "import pyarrow as pa\n\n\n"
        "def run(ctx):\n"
        f"{prelude}"
        "    return pa.table({\n"
        f"        'rate_date': pa.array({dates}, pa.date32()),\n"
        f"        'base': {bases},\n"
        "        'rate': pa.array([Decimal('1.081200'), Decimal('0.861000')], pa.decimal128(12, 6)),\n"
        "    })\n"
    )


SAME_DAY = "[datetime.date(2026, 10, 8), datetime.date(2026, 10, 8)]"
TWO_KEYS = _script(SAME_DAY, "['EUR', 'GBP']")
REPEATED_KEY = _script(SAME_DAY, "['EUR', 'EUR']")
NULL_KEY = _script("[None, datetime.date(2026, 10, 8)]", "['EUR', 'GBP']")
UPSERT = {"mode_type": "upsert", "primary_key": ["rate_date", "base"]}


def _repo(tmp_path, script=TWO_KEYS, **node_extra):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "contracts").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "fx.py").write_text(script)
    node = {
        "schema": "fx", "table": "fx_rates", "owner": "m", "schedule": "hourly",
        "criticality": "SECONDARY", "kind": "python-api", "script": "scripts/fx.py",
        "output_columns": FX_COLUMNS, **node_extra,
    }
    (tmp_path / "contracts" / "fx.yml").write_text(yaml.safe_dump({"nodes": [node]}))
    return tmp_path


def _env(repo, **over):
    return {
        "NODE_ID": "python-api.svc.fx.fx_rates", "TABLE_NAME": "fx_rates",
        "TARGET_SCHEMA": "fx", "CONTRACT_DIR": str(repo / "contracts"), "APP_ROOT": str(repo),
        **over,
    }


def _message(out: str) -> str:
    return json.loads(out.split("BEGIN===\n")[1].split("\n===CONTINUO")[0])["message"]


def test_a_replace_node_still_writes_through_load(tmp_path):
    adapter = FakeWarehouseAdapter()
    assert run_node(_env(_repo(tmp_path)), adapter=adapter) == 0
    assert adapter.loaded[:2] == ("fx", "fx_rates")
    assert adapter.loaded[2].num_rows == 2


def test_an_append_node_appends_and_never_loads(tmp_path):
    adapter = FakeAppendAdapter()
    assert run_node(_env(_repo(tmp_path, mode_type="append")), adapter=adapter) == 0
    assert adapter.appended[:2] == ("fx", "fx_rates")
    assert adapter.appended[2].num_rows == 2
    assert adapter.loaded is None


def test_an_upsert_node_passes_its_key_to_ensure_table_and_upsert(tmp_path):
    adapter = FakeUpsertAdapter()
    assert run_node(_env(_repo(tmp_path, **UPSERT)), adapter=adapter) == 0
    assert adapter.ensured_primary_key == ("rate_date", "base")
    assert adapter.upserted[:2] == ("fx", "fx_rates")
    assert adapter.upserted[3] == ("rate_date", "base")
    assert adapter.loaded is None


def test_append_without_the_capability_fails_before_the_script_runs(tmp_path, capsys):
    marker = tmp_path / "ran"
    script = _script(
        SAME_DAY, "['EUR', 'GBP']", prelude=f"    pathlib.Path({str(marker)!r}).write_text('ran')\n"
    )
    adapter = FakeWarehouseAdapter()
    assert run_node(_env(_repo(tmp_path, script=script, mode_type="append")), adapter=adapter) == 1
    assert _message(capsys.readouterr().out) == "LoadError: FakeWarehouseAdapter does not support append"
    assert not marker.exists()
    assert adapter.ensured is None and adapter.loaded is None


def test_upsert_on_an_append_only_adapter_is_a_load_error(tmp_path, capsys):
    adapter = FakeAppendAdapter()
    assert run_node(_env(_repo(tmp_path, **UPSERT)), adapter=adapter) == 1
    assert _message(capsys.readouterr().out) == "LoadError: FakeAppendAdapter does not support upsert"
    assert adapter.ensured is None and adapter.appended is None


class _PortOnlyAdapter(WarehouseAdapter):
    """A bring-your-own adapter that implements the port and no capability."""

    def __init__(self):
        self.calls = []

    @classmethod
    def required_env(cls):
        return []

    @classmethod
    def from_env(cls):
        return cls()

    def ensure_schema(self, schema):
        self.calls.append("ensure_schema")

    def drop_schema(self, schema):
        self.calls.append("drop_schema")

    def build_empty_from_sql(self, schema, table, compiled_sql):
        self.calls.append("build_empty_from_sql")

    def clone_empty_from_prod(self, candidate_schema, prod_schema, table):
        self.calls.append("clone_empty_from_prod")

    def build_empty_from_columns(self, schema, table, columns, config):
        self.calls.append("build_empty_from_columns")

    def check_binds(self, sql):
        self.calls.append("check_binds")

    def fetch(self, sql):
        self.calls.append("fetch")

    def ensure_table(self, schema, table, columns, *, config):
        self.calls.append("ensure_table")

    def load(self, schema, table, data):
        self.calls.append("load")

    def close(self):
        self.calls.append("close")


def test_a_port_only_byo_adapter_still_replaces_and_cannot_append(tmp_path, capsys):
    replacing = _PortOnlyAdapter()
    assert run_node(_env(_repo(tmp_path / "a")), adapter=replacing) == 0
    assert replacing.calls == ["ensure_table", "load", "close"]

    appending = _PortOnlyAdapter()
    capsys.readouterr()
    assert run_node(_env(_repo(tmp_path / "b", mode_type="append")), adapter=appending) == 1
    assert _message(capsys.readouterr().out) == "LoadError: _PortOnlyAdapter does not support append"
    assert appending.calls == ["close"]


def test_a_repeated_key_is_a_conform_error_and_nothing_is_written(tmp_path, capsys):
    adapter = FakeUpsertAdapter()
    assert run_node(_env(_repo(tmp_path, script=REPEATED_KEY, **UPSERT)), adapter=adapter) == 1
    assert _message(capsys.readouterr().out).startswith(
        "ConformError: primary key (rate_date, base) is not unique in the rows to write: "
        "(2026-10-08, EUR) appears 2 times"
    )
    assert adapter.upserted is None


def test_a_null_key_is_a_conform_error_before_ensure_table(tmp_path, capsys):
    adapter = FakeUpsertAdapter()
    assert run_node(_env(_repo(tmp_path, script=NULL_KEY, **UPSERT)), adapter=adapter) == 1
    assert _message(capsys.readouterr().out) == (
        "ConformError: column rate_date is declared nullable: false but contains nulls"
    )
    assert adapter.ensured is None and adapter.upserted is None


def test_an_engine_error_during_upsert_is_a_load_error(tmp_path, capsys):
    class Deadlocking(FakeUpsertAdapter):
        def _upsert(self, schema, table, data, primary_key):
            raise RuntimeError("deadlock detected")

    assert run_node(_env(_repo(tmp_path, **UPSERT)), adapter=Deadlocking()) == 1
    assert _message(capsys.readouterr().out) == "LoadError: failed to write fx.fx_rates: deadlock detected"


def test_a_value_error_from_the_engine_is_not_taken_for_a_repeated_key(tmp_path, capsys):
    class Overflowing(FakeUpsertAdapter):
        def _upsert(self, schema, table, data, primary_key):
            raise ValueError("numeric field overflow")

    assert run_node(_env(_repo(tmp_path, **UPSERT)), adapter=Overflowing()) == 1
    assert _message(capsys.readouterr().out) == (
        "LoadError: failed to write fx.fx_rates: numeric field overflow"
    )
