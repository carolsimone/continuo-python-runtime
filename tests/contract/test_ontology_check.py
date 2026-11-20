"""Check a Python service's contracts against the company's ontology file, as `validate --ontology` does."""

import ast
from pathlib import Path

import pytest
from continuo_contracts import yaml_strict
from continuo_contracts.ontology_check import Finding

import continuo_python_runtime
from continuo_python_runtime.contract.ontology_check import check_service
from continuo_python_runtime.errors import ContractError
from tests.conftest import NODE_T, NODE_T_BOUND, ONTOLOGY_SERVICE
from tests.odcs_builder import contract_text

UNBOUND_COLUMN = {"name": "extra", "type": "INTEGER"}


def _doc(**over):
    """The document of the contract of NODE_T_BOUND changed by ``over``."""
    return yaml_strict.load(contract_text(**{**NODE_T_BOUND, **over}))


def _check(ontology_file, *docs, service=ONTOLOGY_SERVICE):
    return check_service(list(docs), service=service, ontology_path=ontology_file)


def _lines(findings):
    return [(f.severity, f.rule, f.subject) for f in findings]


def test_a_bound_contract_of_a_declared_service_has_no_finding(ontology_file):
    assert _check(ontology_file, _doc()) == []


def test_the_service_is_the_one_given_not_one_the_contract_names(ontology_file):
    """The contract writes no `service`; it is checked as the service given."""
    assert _check(ontology_file, _doc(), service="billing-py") != []
    assert _check(ontology_file, _doc(service="billing-py")) == []


def test_an_entity_that_is_not_a_thing_is_an_error(ontology_file):
    (finding,) = _check(ontology_file, _doc(entity="Ghost"))
    assert isinstance(finding, Finding)
    assert (finding.severity, finding.rule, finding.subject, finding.contract_id) == (
        "error", "contract", "analytics.t", "analytics.t",
    )
    assert "Ghost" in finding.message


def test_an_unbound_column_is_a_warning(ontology_file):
    columns = [*NODE_T_BOUND["output_columns"], UNBOUND_COLUMN]
    (finding,) = _check(ontology_file, _doc(output_columns=columns))
    assert (finding.severity, finding.rule, finding.subject) == ("warning", "coverage", "analytics.t")
    assert "'extra'" in finding.message


def test_a_contract_of_an_undeclared_service_is_one_warning_for_its_bindings_and_one_information(ontology_file):
    warning, information = _check(ontology_file, _doc(), service="billing-py")
    assert (information.severity, information.rule, information.subject, information.message) == (
        "info", "contract", "billing-py",
        "not declared in the ontology: none of this service's contracts is checked",
    )
    assert (warning.severity, warning.rule, warning.subject) == ("warning", "contract", "analytics.t")


def test_a_contract_of_an_undeclared_service_with_no_binding_is_one_information(ontology_file):
    doc = yaml_strict.load(contract_text(**NODE_T))
    (finding,) = _check(ontology_file, doc, service="billing-py")
    assert (finding.severity, finding.subject) == ("info", "billing-py")


def test_the_findings_of_every_contract_come_back_in_the_order_the_library_gives(ontology_file):
    other = {**NODE_T_BOUND, "table": "u", "entity": "Ghost"}
    findings = _check(ontology_file, _doc(entity="Phantom"), yaml_strict.load(contract_text(**other)))
    assert _lines(findings) == [("error", "contract", "analytics.t"), ("error", "contract", "analytics.u")]


def test_the_documents_are_not_changed(ontology_file):
    doc = _doc()
    before = yaml_strict.load(contract_text(**NODE_T_BOUND))
    _check(ontology_file, doc)
    assert doc == before


def test_a_missing_ontology_file_is_a_contract_error_naming_it(tmp_path):
    missing = tmp_path / "nope.yaml"
    with pytest.raises(ContractError) as exc:
        _check(missing, _doc())
    assert str(exc.value) == f"{missing}: No such file or directory"


def test_a_directory_is_a_contract_error_naming_it(tmp_path):
    with pytest.raises(ContractError) as exc:
        _check(tmp_path, _doc())
    assert str(exc.value) == f"{tmp_path}: Is a directory"


def test_a_file_that_is_not_utf8_is_a_contract_error_naming_it(tmp_path):
    file = tmp_path / "ontology.yaml"
    file.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ContractError, match=rf"^{file}: the file is not UTF-8 text"):
        _check(file, _doc())


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("principals:\n  servces: {}\n", "principals.servces: unknown key, the keys of the principals are "),
        ("- a list\n", "<root>: must be a mapping, got a list"),
        ("principals: {}\nprincipals: {}\n", "<root>: line 2, column 1: "),
    ],
)
def test_a_file_that_is_not_an_ontology_is_a_contract_error_with_the_library_message(tmp_path, text, reason):
    file = tmp_path / "ontology.yaml"
    file.write_text(text, encoding="utf-8")
    with pytest.raises(ContractError) as exc:
        _check(file, _doc())
    assert str(exc.value).startswith(f"{file}: {reason}")
    assert "\n" not in str(exc.value)


def test_a_blank_service_is_a_contract_error(ontology_file):
    with pytest.raises(ContractError, match="the service of a release is not empty or blank"):
        _check(ontology_file, _doc(), service=" ")


def test_only_this_module_imports_the_ontology_modules_of_the_library():
    root = Path(continuo_python_runtime.__file__).parent
    importers = set()
    for file in root.rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if any(name.startswith("continuo_contracts.ontology") for name in names):
                importers.add(file.relative_to(root).as_posix())
    assert importers == {"contract/ontology_check.py"}
