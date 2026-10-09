from continuo_python_runtime.contract.model import (
    DEFAULT_KIND,
    DEFAULT_MODE_TYPE,
    KINDS,
    MODE_TYPES,
    Column,
    Node,
)


def _node(**over):
    base = dict(
        schema="analytics",
        table="t",
        owner="marketing",
        schedule="daily",
        criticality="SECONDARY",
        script="scripts/t.py",
        reads={"ids": "select id from analytics.a"},
        output_columns=(Column("id", "INTEGER", nullable=False),),
    )
    base.update(over)
    return Node(**base)


def test_defaults_and_relation():
    n = _node()
    assert n.relation == "analytics.t"
    assert n.extra_columns == "raise"
    assert n.description == ""
    assert n.content_hash is None
    assert n.output_columns[0].nullable is False
    assert n.config == {}


def test_config_accepts_nested_mapping():
    n = _node(config={"indexes": [{"columns": ["id"], "unique": True}]})
    assert n.config == {"indexes": [{"columns": ["id"], "unique": True}]}


def test_kind_defaults_to_python_node():
    n = _node()
    assert n.kind == "python-node" == DEFAULT_KIND


def test_kind_accepts_python_csv():
    n = _node(kind="python-csv")
    assert n.kind == "python-csv"


def test_kinds_frozenset_contains_every_kind():
    assert KINDS == frozenset({"python-node", "python-csv", "python-api"})


def test_mode_types_are_the_closed_vocabulary():
    assert MODE_TYPES == frozenset({"append", "upsert", "replace"})
    assert DEFAULT_MODE_TYPE == "replace"


def test_mode_type_defaults_to_replace_with_no_key():
    n = _node()
    assert n.mode_type == "replace"
    assert n.primary_key == ()


def test_an_upsert_node_keeps_its_key_as_written_in_key_order():
    n = _node(
        output_columns=(
            Column("rate_date", "DATE", nullable=False),
            Column("base", "VARCHAR(3)", nullable=False),
        ),
        mode_type="upsert",
        primary_key=("base", "rate_date"),
    )
    assert n.mode_type == "upsert"
    assert n.primary_key == ("base", "rate_date")
