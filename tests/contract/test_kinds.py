import pytest

from continuo_python_runtime.contract.kinds import RULES, CsvRead, NoReads, SqlReads, rules_for
from continuo_python_runtime.contract.model import KINDS
from continuo_python_runtime.errors import ContractError
from tests.odcs_builder import contract_node


def test_every_kind_has_rules():
    assert set(RULES) == KINDS


def test_python_node_reads_sql():
    assert isinstance(RULES["python-node"].reads, SqlReads)


def test_python_csv_reads_one_csv_uri():
    assert isinstance(RULES["python-csv"].reads, CsvRead)


@pytest.mark.parametrize("kind", ["PYTHON-NODE", " python-node", "python-model", "", None, 3])
def test_kind_is_case_and_whitespace_sensitive(kind):
    with pytest.raises(ContractError, match="'kind' must be one of"):
        rules_for(kind, "t.yml (a.b)")


def test_sql_reads_rejects_an_empty_mapping():
    with pytest.raises(ContractError, match="'reads' must be a non-empty mapping of name -> SQL"):
        SqlReads().validate({}, "L", dialect=None, check_reads=True)


def test_sql_reads_skips_the_shape_gate_when_asked():
    reads = {"a": "this is not sql at all"}
    assert SqlReads().validate(reads, "L", dialect=None, check_reads=False) == reads


def test_sql_reads_accepts_reads_that_name_their_schemas():
    reads = {"a": "select * from raw.a", "b": "with c as (select 1) select * from c join raw.b on 1=1"}
    assert SqlReads().validate(reads, "L", dialect="postgres", check_reads=True) == reads


def test_an_unqualified_table_fails_validate():
    with pytest.raises(ContractError) as exc:
        SqlReads().validate({"x": "select * from orders"}, "L", dialect=None, check_reads=True)
    assert str(exc.value) == "L: read 'x' names table 'orders' without its schema; write <schema>.orders"


def test_validate_names_the_first_read_and_the_first_table_that_lacks_a_schema():
    reads = {"a": "select * from raw.a", "b": "select * from raw.b join y on 1=1 join z on 1=1", "c": "select * from w"}
    with pytest.raises(ContractError) as exc:
        SqlReads().validate(reads, "L", dialect="postgres", check_reads=True)
    assert str(exc.value) == "L: read 'b' names table 'y' without its schema; write <schema>.y"


def test_a_cte_in_one_read_does_not_hide_the_same_name_in_another():
    reads = {"a": "with users as (select 1) select * from users", "b": "select * from users"}
    with pytest.raises(ContractError, match="read 'b' names table 'users' without its schema"):
        SqlReads().validate(reads, "L", dialect="postgres", check_reads=True)


def test_a_nameless_table_with_no_schema_is_named_by_its_sql_text():
    with pytest.raises(ContractError) as exc:
        SqlReads().validate(
            {"x": "select * from generate_series(1, 3)"}, "L", dialect="postgres", check_reads=True
        )
    assert str(exc.value) == (
        "L: read 'x' names table 'generate_series(1, 3)' without its schema; "
        "write <schema>.generate_series(1, 3)"
    )


def test_a_schema_qualified_function_call_passes_validate():
    reads = {"x": "select * from pg_catalog.generate_series(1, 3)"}
    assert SqlReads().validate(reads, "L", dialect="postgres", check_reads=True) == reads


def test_the_single_read_check_comes_before_the_schema_rule():
    with pytest.raises(ContractError, match="'reads.x' must be a single read query"):
        SqlReads().validate({"x": "select 1 from t; select 2 from u"}, "L", dialect=None, check_reads=True)


@pytest.mark.parametrize("dialect", ["postgres", None])
@pytest.mark.parametrize("sql", ["SELECT FROM WHERE (", "; select * from raw.a", ";"])
def test_a_read_sqlglot_refuses_is_a_contract_error_not_a_sqlglot_error(sql, dialect):
    """A leading `;` passes ensure_single_read (one statement is left) but not the
    parse the schema rule needs, which topology-controller refuses as well."""
    with pytest.raises(ContractError, match=r"^L: 'reads\.x' must be a single read query \("):
        SqlReads().validate({"x": sql}, "L", dialect=dialect, check_reads=True)


def test_the_harness_does_not_apply_the_rule():
    reads = {"x": "select * from orders"}
    assert SqlReads().validate(reads, "L", dialect=None, check_reads=False) == reads


def test_a_contract_with_an_unqualified_read_loads_as_the_harness_reads_it():
    """The harness loads with check_reads=False, so the rule is validate's alone."""
    node = contract_node(
        schema="analytics",
        table="t",
        owner="marketing",
        schedule="daily",
        criticality="SECONDARY",
        script="scripts/t.py",
        reads={"x": "select * from orders"},
        output_columns=[{"name": "id", "type": "INTEGER"}],
    )
    assert node.reads == {"x": "select * from orders"}


def test_csv_read_requires_exactly_a_csv_key():
    with pytest.raises(ContractError, match=r"a python-csv node's 'reads' must be exactly \{csv: <uri>\}"):
        CsvRead().validate({"csv": "s3://b/k", "x": "y"}, "L", dialect=None, check_reads=True)


def test_csv_read_validates_the_uri():
    with pytest.raises(ContractError, match="invalid csv uri"):
        CsvRead().validate({"csv": "http://insecure/x.csv"}, "L", dialect=None, check_reads=True)


def test_python_api_declares_no_reads():
    assert isinstance(RULES["python-api"].reads, NoReads)


@pytest.mark.parametrize("reads", [None, {}])
def test_no_reads_accepts_absent_or_empty(reads):
    assert NoReads().validate(reads, "L", dialect=None, check_reads=True) == {}


@pytest.mark.parametrize("reads", [{"x": "select 1"}, {"csv": "s3://b/k"}, [], "select 1"])
def test_no_reads_rejects_any_declared_read(reads):
    with pytest.raises(ContractError, match="a python-api node declares no 'reads'"):
        NoReads().validate(reads, "L", dialect=None, check_reads=True)
