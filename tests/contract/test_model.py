from continuo_python_runtime.contract.model import Column, Node


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
