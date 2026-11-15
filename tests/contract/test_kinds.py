import pytest

from continuo_python_runtime.contract.kinds import RULES, CsvRead, NoReads, SqlReads, rules_for
from continuo_python_runtime.contract.model import KINDS
from continuo_python_runtime.errors import ContractError


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
