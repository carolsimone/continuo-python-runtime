import pytest

from continuo_python_runtime.contract.kinds import RULES, CsvRead, SqlReads, rules_for
from continuo_python_runtime.contract.model import KINDS
from continuo_python_runtime.errors import ContractError


def test_every_kind_has_rules():
    assert set(RULES) == KINDS


def test_python_node_requires_a_script_and_sql_reads():
    rules = RULES["python-node"]
    assert rules.script_required is True
    assert isinstance(rules.reads, SqlReads)


def test_python_csv_forbids_a_script_and_reads_one_csv_uri():
    rules = RULES["python-csv"]
    assert rules.script_required is False
    assert isinstance(rules.reads, CsvRead)


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
