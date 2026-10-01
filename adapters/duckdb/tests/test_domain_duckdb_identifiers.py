"""Identifier value objects: validation only; quoting is an infrastructure concern."""
import pytest

from continuo_duckdb_adapter.domain.identifiers import Identifier, QualifiedTable


@pytest.mark.parametrize("name", ["orders", "Order Table", 'we"ird', "50%", "select", "lake", "ünï"])
def test_identifier_accepts_any_non_empty_text(name):
    assert Identifier(name).name == name


@pytest.mark.parametrize("bad", ["", None, 7, "a\x00b"])
def test_identifier_rejects_empty_non_string_and_nul(bad):
    with pytest.raises(ValueError):
        Identifier(bad)


def test_identifiers_are_value_objects():
    assert Identifier("a") == Identifier("a")
    assert hash(Identifier("a")) == hash(Identifier("a"))


def test_qualified_table_of_builds_both_parts():
    table = QualifiedTable.of("analytics", "orders")
    assert table.schema == Identifier("analytics")
    assert table.table == Identifier("orders")
