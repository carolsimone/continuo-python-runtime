"""The contract loader: one ODCS contract per node, read from a contracts folder."""

import re
import shutil
from pathlib import Path

import pytest
from continuo_contracts import yaml_strict
from continuo_contracts.python import node_from_odcs

from continuo_python_runtime.contract.loader import load_contract_dir, node_from_contract
from continuo_python_runtime.contract.model import Column
from continuo_python_runtime.errors import ContractError
from continuo_python_runtime.types import parse_sql_type
from tests.odcs_builder import contract_text, write_contract

DEMO = Path(__file__).resolve().parent.parent / "fixtures" / "demo" / "service-py"
OLD_KPIS = (DEMO / "old" / "py_daily_kpis.yml").read_text(encoding="utf-8")

FIELDS = {
    "schema": "analytics",
    "table": "t",
    "owner": "marketing",
    "schedule": "daily",
    "criticality": "SECONDARY",
    "script": "scripts/t.py",
    "reads": {"ids": "select id from analytics.a"},
    "output_columns": [
        {"name": "id", "type": "INTEGER", "nullable": False},
        {"name": "amount", "type": "NUMERIC(10,2)"},
    ],
}


def _node(*, dialect=None, check_reads=True, **over):
    """The node the loader reads from the contract of FIELDS changed by ``over``."""
    fields = {**FIELDS, **over}
    return node_from_contract(
        yaml_strict.load(contract_text(**fields)),
        Path(f"{fields['table']}.odcs.yaml"),
        dialect=dialect,
        check_reads=check_reads,
    )


def _with_read(sql, **kwargs):
    return _node(reads={"ids": sql}, **kwargs)


# --- the folder -----------------------------------------------------------------


def test_the_demo_contracts_load(demo_contracts):
    nodes = load_contract_dir(demo_contracts, dialect="postgres")
    assert [n.relation for n in nodes] == [
        "analytics.demo_orders_csv", "analytics.fx_eur_usd_rates", "analytics.py_daily_kpis",
    ]
    kpis = nodes[2]
    assert (kpis.owner, kpis.schedule, kpis.criticality, kpis.kind) == (
        "service-py", "daily", "SECONDARY", "python-node",
    )
    assert kpis.reads == {"daily": "select created_at, amount_eur from analytics.daily_transactions"}
    assert kpis.python.table == "py_daily_kpis"


def test_each_node_keeps_its_file_and_the_library_node(demo_contracts):
    csv, api, kpis = load_contract_dir(demo_contracts)
    for node in (csv, api, kpis):
        assert node.path == demo_contracts / f"{node.table}.odcs.yaml"
        assert node.python == node_from_odcs(yaml_strict.load(node.path.read_text(encoding="utf-8")))
    assert (csv.kind, csv.script, csv.reads) == (
        "python-csv", "", {"csv": "s3://continuo-dev/static-files/demo/orders.csv"},
    )
    assert (api.kind, api.script, api.reads, api.secret_ref) == (
        "python-api", "scripts/fx_eur_usd_rates.py", {}, "continuo-api-demo-fx",
    )


def test_an_old_nodes_file_is_refused_with_how_to_convert_it(tmp_path):
    (tmp_path / "py_daily_kpis.yml").write_text(OLD_KPIS)
    with pytest.raises(
        ContractError,
        match=r"py_daily_kpis\.yml: runtime 0\.13\.0 reads only ODCS contracts named <table>\.odcs\.yaml",
    ):
        load_contract_dir(tmp_path)


def _refusal(file):
    return (
        f"{file}: runtime 0.13.0 reads only ODCS contracts named "
        "<table>.odcs.yaml; convert this file ("
        "https://github.com/carolsimone/continuo-python-runtime/blob/main/"
        "docs/boundary-contract.md#converting-a-contract-file)"
    )


@pytest.mark.parametrize("name", ["py_daily_kpis.yml", "nodes.yaml"])
def test_an_old_file_beside_odcs_contracts_is_refused(demo_contracts, name):
    (demo_contracts / name).write_text(OLD_KPIS)
    with pytest.raises(ContractError) as exc:
        load_contract_dir(demo_contracts)
    assert str(exc.value) == _refusal(demo_contracts / name)


@pytest.mark.parametrize("name", ["legacy.YML", "t.odcs.YAML", "notes.Yaml"])
def test_an_extension_in_another_case_is_refused_too(demo_contracts, name):
    (demo_contracts / name).write_text(OLD_KPIS)
    with pytest.raises(ContractError) as exc:
        load_contract_dir(demo_contracts)
    assert str(exc.value) == _refusal(demo_contracts / name)


def test_every_file_in_another_format_is_named(demo_contracts):
    leftovers = ["nodes.yaml", "legacy.YML", "py_daily_kpis.odcs.yml"]
    for name in leftovers:
        (demo_contracts / name).write_text(OLD_KPIS)
    with pytest.raises(ContractError) as exc:
        load_contract_dir(demo_contracts)
    assert str(exc.value).splitlines() == [
        _refusal(demo_contracts / name) for name in sorted(leftovers)
    ]


def test_a_dot_odcs_dot_yml_file_is_refused(demo_contracts):
    kpis = demo_contracts / "py_daily_kpis.odcs.yaml"
    kpis.rename(demo_contracts / "py_daily_kpis.odcs.yml")
    with pytest.raises(
        ContractError,
        match=r"py_daily_kpis\.odcs\.yml: runtime 0\.13\.0 reads only ODCS contracts named <table>\.odcs\.yaml",
    ):
        load_contract_dir(demo_contracts)


def test_a_file_that_is_not_yaml_is_not_read(demo_contracts):
    (demo_contracts / "README.md").write_text("# contracts\n")
    (demo_contracts / "notes.json").write_text("{}\n")
    assert len(load_contract_dir(demo_contracts)) == 3


def test_two_files_for_one_table_are_refused(demo_contracts):
    first = demo_contracts / "analytics.py_daily_kpis.odcs.yaml"
    second = demo_contracts / "py_daily_kpis.odcs.yaml"
    shutil.copyfile(second, first)
    with pytest.raises(ContractError) as exc:
        load_contract_dir(demo_contracts)
    assert str(exc.value) == f"duplicate node analytics.py_daily_kpis (in {first} and {second})"


def test_two_files_for_one_table_are_refused_when_reads_are_not_checked(demo_contracts):
    shutil.copyfile(
        demo_contracts / "py_daily_kpis.odcs.yaml", demo_contracts / "analytics.py_daily_kpis.odcs.yaml"
    )
    with pytest.raises(ContractError, match="duplicate node analytics.py_daily_kpis"):
        load_contract_dir(demo_contracts, check_reads=False)


def _mixed_case_pair(demo_contracts):
    """Two files for one table that differ only in the case of its name.

    The first holds the demo's ``py_daily_kpis`` contract as ``<table>.odcs.yaml``;
    the second is the same contract with the table named ``Py_Daily_Kpis``, in the
    ``<schema>.<table>`` form (two plain ``<table>`` files that differ only in case
    cannot coexist on a case-insensitive filesystem). Its id stays lower case, which
    the library accepts because it compares the id and the table in lower case.
    """
    plain = demo_contracts / "py_daily_kpis.odcs.yaml"
    mixed = demo_contracts / "analytics.Py_Daily_Kpis.odcs.yaml"
    text = plain.read_text(encoding="utf-8")
    mixed.write_text(
        text.replace("name: py_daily_kpis", "name: Py_Daily_Kpis"), encoding="utf-8"
    )
    return plain, mixed


def test_two_files_for_one_table_in_another_case_are_refused(demo_contracts):
    plain, mixed = _mixed_case_pair(demo_contracts)
    with pytest.raises(ContractError) as exc:
        load_contract_dir(demo_contracts)
    assert str(exc.value) == f"duplicate node analytics.py_daily_kpis (in {mixed} and {plain})"


def test_the_mixed_case_table_is_a_node_of_its_own_in_the_library(demo_contracts):
    """The library accepts the mixed-case table, so the loader's check alone refuses the pair."""
    _, mixed = _mixed_case_pair(demo_contracts)
    node = node_from_contract(
        yaml_strict.load(mixed.read_text(encoding="utf-8")), mixed, dialect=None, check_reads=False
    )
    assert node.relation == "analytics.Py_Daily_Kpis"


def test_the_conversion_link_names_a_heading_of_the_boundary_contract():
    """The refusal's link ends in the anchor of a heading that docs/boundary-contract.md has."""
    docs = (DEMO.parents[3] / "docs" / "boundary-contract.md").read_text(encoding="utf-8")
    assert "\n### Converting a contract file\n" in docs
    assert _refusal("t.yml").endswith("/docs/boundary-contract.md#converting-a-contract-file)")


def test_a_folder_without_contracts_is_refused(tmp_path):
    with pytest.raises(ContractError, match=f"no contract files found in {re.escape(str(tmp_path))}"):
        load_contract_dir(tmp_path)


def test_every_library_issue_is_listed(tmp_path):
    file = write_contract(tmp_path, **{**FIELDS, "criticality": "HIGH", "timeout_minutes": 0})
    with pytest.raises(ContractError) as exc:
        load_contract_dir(tmp_path)
    lines = str(exc.value).splitlines()
    assert len(lines) == 2
    assert all(line.startswith(f"{file}: customProperties[") for line in lines)
    assert "`criticality` is 'HIGH'" in lines[0]
    assert "`timeoutMinutes` is 0" in lines[1]


def test_a_strict_yaml_error_names_the_file_and_line(tmp_path):
    file = tmp_path / "t.odcs.yaml"
    file.write_text("apiVersion: v3.2.0\nkind: DataContract\nkind: DataContract\n")
    with pytest.raises(ContractError) as exc:
        load_contract_dir(tmp_path)
    assert str(exc.value) == f"{file}: line 3, column 1: duplicate key 'kind'"


@pytest.mark.parametrize(("text", "holds"), [("", "nothing"), ("- a\n- b\n", "a list")])
def test_a_file_that_holds_no_contract_is_refused(tmp_path, text, holds):
    file = tmp_path / "t.odcs.yaml"
    file.write_text(text)
    with pytest.raises(ContractError) as exc:
        load_contract_dir(tmp_path)
    assert str(exc.value) == f"{file}: a contract file holds one ODCS contract, a mapping; this one holds {holds}"


def test_a_file_that_is_not_utf8_is_refused(tmp_path):
    file = tmp_path / "t.odcs.yaml"
    file.write_bytes(contract_text(**FIELDS).encode("utf-8") + b"# caf\xe9\n")
    with pytest.raises(ContractError, match=rf"^{re.escape(str(file))}: the file is not UTF-8 text"):
        load_contract_dir(tmp_path)


# --- the node's fields ------------------------------------------------------------


def test_a_contract_maps_onto_the_node(tmp_path):
    write_contract(
        tmp_path,
        schema="fx", table="fx_rates", owner="markets", schedule="hourly", criticality="CORE",
        kind="python-api", script="scripts/fx.py", description="EUR rates",
        extra_columns="warn", config={"indexes": [{"columns": ["base"], "unique": True}]},
        secret_ref="continuo-api-fx", mode_type="upsert", primary_key=["rate_date", "base"],
        timeout_minutes=120, assets=["data/ref.json"],
        output_columns=[
            {"name": "rate_date", "type": "DATE", "nullable": False},
            {"name": "base", "type": "VARCHAR(3)", "nullable": False},
            {"name": "rate", "type": "decimal(12, 6)"},
        ],
    )
    (node,) = load_contract_dir(tmp_path)
    assert node.relation == "fx.fx_rates"
    assert (node.owner, node.description, node.schedule, node.criticality) == (
        "markets", "EUR rates", "hourly", "CORE",
    )
    assert (node.kind, node.script, node.reads, node.secret_ref) == (
        "python-api", "scripts/fx.py", {}, "continuo-api-fx",
    )
    assert node.output_columns == (
        Column("rate_date", "DATE", nullable=False),
        Column("base", "VARCHAR(3)", nullable=False),
        Column("rate", "decimal(12, 6)", nullable=True),
    )
    assert node.extra_columns == "warn"
    assert node.config == {"indexes": [{"columns": ["base"], "unique": True}]}
    assert (node.mode_type, node.primary_key) == ("upsert", ("rate_date", "base"))
    assert node.timeout_minutes == 120
    assert node.assets == ("data/ref.json",)


def test_left_out_fields_take_their_defaults():
    node = _node()
    assert node.description == ""
    assert node.kind == "python-node"
    assert node.extra_columns == "raise"
    assert node.config == {}
    assert node.secret_ref == ""
    assert (node.mode_type, node.primary_key) == ("replace", ())
    assert node.timeout_minutes is None
    assert node.assets == ()


def test_an_upsert_key_comes_in_the_order_of_its_positions():
    columns = [
        {"name": "rate_date", "type": "DATE", "nullable": False},
        {"name": "base", "type": "VARCHAR(3)", "nullable": False},
    ]
    node = _node(output_columns=columns, mode_type="upsert", primary_key=["base", "rate_date"])
    assert node.primary_key == ("base", "rate_date")


def test_the_config_keeps_a_key_no_engine_reads():
    """The loader stays engine-blind: the engine's adapter owns the config vocabulary."""
    assert _node(config={"sortkey": ["id"]}).config == {"sortkey": ["id"]}


# Spellings in and around the column-type grammar. The loader does not parse a
# column type itself: the library's grammar decides, and the harness maps each
# type to an Arrow type through parse_sql_type. These pin the two together, so
# CI accepts no column type the harness cannot write.
ACCEPTED_COLUMN_TYPES = [
    "BIGINT", "int", "INTEGER", "DOUBLE PRECISION", "double precision", "TEXT", "TIMESTAMP",
    "date", "BOOLEAN", "NUMERIC(10,2)", "DECIMAL(5, 2)", "numeric(38,38)", "VARCHAR(255)",
    "CHAR(1)", " INTEGER ", "NUMERIC(012,2)", "INTEGER\n",
]
REFUSED_COLUMN_TYPES = [
    "NUMERIC(39,0)", "NUMERIC(5,6)", "NUMERIC(0,0)", "VARCHAR(0)", "VARCHAR", "CHAR",
    "DOUBLE  PRECISION", "REAL", "FLOAT", "SMALLINT", "JSONB", "UUID", "TIMESTAMPTZ",
    "TIMESTAMP WITH TIME ZONE", "BLOB", "INTEGER;", "ınt",
]


def _with_column_type(sql_type):
    return _node(output_columns=[{"name": "c", "type": sql_type}])


@pytest.mark.parametrize("sql_type", ACCEPTED_COLUMN_TYPES)
def test_a_column_type_the_library_accepts_the_harness_parses(sql_type):
    node = _with_column_type(sql_type)
    assert node.output_columns == (Column("c", sql_type, nullable=True),)
    parse_sql_type(sql_type)


@pytest.mark.parametrize("sql_type", REFUSED_COLUMN_TYPES)
def test_a_column_type_outside_the_grammar_is_refused(sql_type):
    with pytest.raises(
        ContractError,
        match=r"^t\.odcs\.yaml: schema\[0\]\.properties\[0\]\.physicalType: .* is not a type continuo accepts",
    ):
        _with_column_type(sql_type)


# --- the reads: one SELECT each, under the dialect ----------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "WITH c AS (select 1) select * from c",
        "select id from analytics.a;",
        "select split_part(tags, ';', 1) as tag from analytics.a",
        "-- note\nselect id from analytics.a",
        "/* note */ select id from analytics.a",
        "(select 1) union all (select 2)",
        "select 'it''s fine' as tag from analytics.a",
    ],
)
def test_a_single_read_query_loads(sql):
    assert _with_read(sql).reads == {"ids": sql}


@pytest.mark.parametrize(
    "sql",
    ["drop table x; select 1", "delete from t", "select 1; select 2;", "select 1; select 2"],
)
def test_a_read_that_is_not_one_select_is_refused(sql):
    with pytest.raises(
        ContractError, match=r"t\.odcs\.yaml \(analytics\.t\): 'reads\.ids' must be a single read query"
    ):
        _with_read(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "/* not closed select id from analytics.a",
        "select 'unterminated from analytics.a; drop table x",
        'select "unterminated from analytics.a; drop table x',
    ],
)
def test_a_read_the_tokenizer_refuses_is_a_contract_error(sql):
    """sqlglot's tokenizer raises TokenError, not ValueError, for these."""
    with pytest.raises(ContractError, match=r"reads\.ids.*Error tokenizing"):
        _with_read(sql)


def test_a_read_that_names_a_table_without_its_schema_is_refused():
    with pytest.raises(ContractError) as exc:
        _with_read("select id from orders")
    assert str(exc.value) == (
        "t.odcs.yaml (analytics.t): read 'ids' names table 'orders' without its schema; "
        "write <schema>.orders"
    )


def test_a_folder_with_a_read_that_names_no_schema_is_refused_by_the_loader(tmp_path):
    file = write_contract(tmp_path, **{**FIELDS, "reads": {"ids": "select id from orders"}})
    with pytest.raises(ContractError, match=rf"{re.escape(str(file))} \(analytics\.t\): read 'ids' names table 'orders'"):
        load_contract_dir(tmp_path)
    (node,) = load_contract_dir(tmp_path, check_reads=False)
    assert node.reads == {"ids": "select id from orders"}


def test_the_dialect_decides_which_reads_parse(tmp_path):
    """QUALIFY parses under sqlglot's dialect-neutral grammar and not under trino's."""
    sql = "select a from analytics.a qualify row_number() over (order by a) = 1"
    write_contract(tmp_path, **{**FIELDS, "reads": {"ids": sql}})
    load_contract_dir(tmp_path)
    with pytest.raises(ContractError, match="reads.ids"):
        load_contract_dir(tmp_path, dialect="trino")


def test_an_unknown_dialect_names_the_flag_not_the_read(tmp_path):
    write_contract(tmp_path, **FIELDS)
    with pytest.raises(ContractError) as exc:
        load_contract_dir(tmp_path, dialect="POSTGRES")
    message = str(exc.value)
    assert "--dialect" in message and "POSTGRES" in message
    assert "reads.ids" not in message


def test_an_unknown_dialect_is_refused_when_reads_are_not_checked(tmp_path):
    write_contract(tmp_path, **FIELDS)
    with pytest.raises(ContractError, match="unknown --dialect"):
        load_contract_dir(tmp_path, dialect="POSTGRESQL-9", check_reads=False)


def _spy_on_ensure_single_read(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "continuo_python_runtime.contract.kinds.ensure_single_read",
        lambda sql, dialect=None: calls.append((sql, dialect)),
    )
    return calls


def test_the_dialect_reaches_the_read_check(monkeypatch, tmp_path):
    calls = _spy_on_ensure_single_read(monkeypatch)
    write_contract(tmp_path, **FIELDS)
    load_contract_dir(tmp_path, dialect="postgres")
    load_contract_dir(tmp_path)
    assert calls == [(FIELDS["reads"]["ids"], "postgres"), (FIELDS["reads"]["ids"], None)]


def test_check_reads_false_skips_only_the_read_check(monkeypatch, tmp_path):
    calls = _spy_on_ensure_single_read(monkeypatch)
    write_contract(tmp_path, **FIELDS)
    (node,) = load_contract_dir(tmp_path, check_reads=False)
    assert calls == []
    assert node.reads == FIELDS["reads"]


def test_check_reads_false_accepts_a_read_the_neutral_parser_refuses():
    """Ordinary postgres (`~`) that sqlglot's dialect-neutral grammar refuses."""
    sql = "select id from analytics.a where b ~ 'x'"
    with pytest.raises(ContractError):
        _with_read(sql)
    assert _with_read(sql, check_reads=False).reads == {"ids": sql}


def test_check_reads_false_still_applies_every_contract_rule(tmp_path):
    file = write_contract(tmp_path, **{**FIELDS, "criticality": "WHENEVER"})
    with pytest.raises(ContractError, match=rf"{re.escape(str(file))}: .*`criticality` is 'WHENEVER'"):
        load_contract_dir(tmp_path, check_reads=False)
