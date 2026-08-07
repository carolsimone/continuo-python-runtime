"""Tests for the contract v1 loader/validator."""

import yaml
import pytest

from continuo_python_runtime.contract.loader import load_contract_dir, parse_node
from continuo_python_runtime.errors import ContractError

VALID = {
    "schema": "analytics",
    "table": "t",
    "description": "d",
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


def test_parse_valid_node():
    n = parse_node(VALID, "f.yml")
    assert n.relation == "analytics.t"
    assert n.output_columns[1].type == "NUMERIC(10,2)"
    assert n.output_columns[0].nullable is False
    assert n.output_columns[1].nullable is True
    assert n.extra_columns == "raise"
    assert n.content_hash is None


def test_parse_valid_node_accepts_content_hash():
    n = parse_node({**VALID, "content_hash": "abc123"}, "f.yml")
    assert n.content_hash == "abc123"


def test_parse_node_without_config_defaults_to_empty_mapping():
    n = parse_node(VALID, "f.yml")
    assert n.config == {}


def test_config_preserves_nested_structure():
    config = {"indexes": [{"columns": ["id"], "unique": True}]}
    n = parse_node({**VALID, "config": config}, "f.yml")
    assert n.config == config


def test_config_empty_mapping_loads():
    n = parse_node({**VALID, "config": {}}, "f.yml")
    assert n.config == {}


def test_config_not_a_mapping_rejected():
    with pytest.raises(ContractError, match="config"):
        parse_node({**VALID, "config": "not-a-mapping"}, "f.yml")


def test_config_non_string_key_rejected():
    with pytest.raises(ContractError, match="config"):
        parse_node({**VALID, "config": {1: "x"}}, "f.yml")


def test_config_nested_non_string_key_rejected():
    with pytest.raises(ContractError, match="config"):
        parse_node({**VALID, "config": {"indexes": [{1: "x"}]}}, "f.yml")


def test_config_allows_unknown_vocabulary_key():
    """The loader stays engine-blind (design §3.3): an unrecognized
    physical-layout key such as 'sortkey' is not this loader's concern.
    The engine adapter enforces its own vocabulary later (Task 6)."""
    n = parse_node({**VALID, "config": {"sortkey": ["id"]}}, "f.yml")
    assert n.config == {"sortkey": ["id"]}


@pytest.mark.parametrize(
    "missing",
    ["schema", "table", "owner", "schedule", "script", "reads", "output_columns"],
)
def test_missing_required_field_rejected(missing):
    raw = {k: v for k, v in VALID.items() if k != missing}
    with pytest.raises(ContractError, match=missing):
        parse_node(raw, "f.yml")


@pytest.mark.parametrize("field", ["schema", "table", "owner", "schedule", "script"])
def test_empty_required_string_rejected(field):
    with pytest.raises(ContractError, match=field):
        parse_node({**VALID, field: ""}, "f.yml")


def test_unknown_key_rejected():
    with pytest.raises(ContractError, match="unknown"):
        parse_node({**VALID, "schdule": "daily"}, "f.yml")


def test_unknown_key_error_names_source_and_node():
    try:
        parse_node({**VALID, "bogus": "x"}, "f.yml")
        pytest.fail("expected ContractError")
    except ContractError as exc:
        assert "f.yml" in str(exc)
        assert "analytics.t" in str(exc)


def test_bad_criticality_and_policy_and_type():
    with pytest.raises(ContractError, match="criticality"):
        parse_node({**VALID, "criticality": "HIGH"}, "f.yml")
    with pytest.raises(ContractError, match="extra_columns"):
        parse_node({**VALID, "extra_columns": "ignore"}, "f.yml")
    with pytest.raises(ContractError, match="type"):
        parse_node(
            {**VALID, "output_columns": [{"name": "x", "type": "JSONB"}]}, "f.yml"
        )


def test_valid_type_grammar_variants():
    cols = [
        {"name": "a", "type": "BIGINT"},
        {"name": "b", "type": "INT"},
        {"name": "c", "type": "DOUBLE PRECISION"},
        {"name": "d", "type": "TEXT"},
        {"name": "e", "type": "TIMESTAMP"},
        {"name": "f", "type": "DATE"},
        {"name": "g", "type": "BOOLEAN"},
        {"name": "h", "type": "DECIMAL(5, 2)"},
        {"name": "i", "type": "VARCHAR(255)"},
        {"name": "j", "type": "CHAR(1)"},
    ]
    n = parse_node({**VALID, "output_columns": cols}, "f.yml")
    assert len(n.output_columns) == len(cols)


def test_duplicate_column_rejected():
    cols = [{"name": "id", "type": "INTEGER"}, {"name": "id", "type": "BIGINT"}]
    with pytest.raises(ContractError, match="duplicate column"):
        parse_node({**VALID, "output_columns": cols}, "f.yml")


def test_empty_output_columns_rejected():
    with pytest.raises(ContractError, match="output_columns"):
        parse_node({**VALID, "output_columns": []}, "f.yml")


def test_output_column_missing_name_rejected():
    with pytest.raises(ContractError, match="name"):
        parse_node({**VALID, "output_columns": [{"type": "INTEGER"}]}, "f.yml")


def test_output_column_missing_type_rejected():
    with pytest.raises(ContractError, match="type"):
        parse_node({**VALID, "output_columns": [{"name": "x"}]}, "f.yml")


def test_output_column_not_mapping_rejected():
    with pytest.raises(ContractError, match="output_columns"):
        parse_node({**VALID, "output_columns": ["id"]}, "f.yml")


def test_empty_reads_rejected():
    with pytest.raises(ContractError, match="reads"):
        parse_node({**VALID, "reads": {}}, "f.yml")


def test_reads_empty_sql_rejected():
    with pytest.raises(ContractError, match="reads"):
        parse_node({**VALID, "reads": {"ids": ""}}, "f.yml")


def test_reads_non_string_sql_rejected():
    with pytest.raises(ContractError, match="reads"):
        parse_node({**VALID, "reads": {"ids": 123}}, "f.yml")


def test_reads_not_mapping_rejected():
    with pytest.raises(ContractError, match="reads"):
        parse_node({**VALID, "reads": ["select 1"]}, "f.yml")


def test_reads_non_string_name_rejected():
    with pytest.raises(ContractError, match="reads"):
        parse_node({**VALID, "reads": {1: "select 1"}}, "f.yml")


def test_reads_blank_name_rejected():
    with pytest.raises(ContractError, match="reads"):
        parse_node({**VALID, "reads": {" ": "select 1"}}, "f.yml")


def test_reads_disallows_non_select_statement():
    with pytest.raises(ContractError, match="reads.ids"):
        parse_node({**VALID, "reads": {"ids": "drop table x; select 1"}}, "f.yml")


def test_reads_disallows_delete():
    with pytest.raises(ContractError, match="reads.ids"):
        parse_node({**VALID, "reads": {"ids": "delete from t"}}, "f.yml")


def test_reads_allows_with_clause():
    n = parse_node(
        {**VALID, "reads": {"ids": "WITH c AS (select 1) select * from c"}}, "f.yml"
    )
    assert n.reads["ids"] == "WITH c AS (select 1) select * from c"


def test_reads_allows_single_trailing_semicolon():
    n = parse_node({**VALID, "reads": {"ids": "select id from analytics.a;"}}, "f.yml")
    assert n.reads["ids"] == "select id from analytics.a;"


def test_reads_disallows_multiple_semicolons():
    with pytest.raises(ContractError, match="reads.ids"):
        parse_node({**VALID, "reads": {"ids": "select 1; select 2;"}}, "f.yml")


def test_reads_allows_semicolon_inside_string_literal():
    """A ';' inside a single-quoted string literal argument must not trip the
    literal-unaware semicolon scan."""
    sql = "select split_part(tags, ';', 1) as tag from analytics.a"
    n = parse_node({**VALID, "reads": {"ids": sql}}, "f.yml")
    assert n.reads["ids"] == sql


def test_reads_allows_leading_line_comment():
    sql = "-- note\nselect id from analytics.a"
    n = parse_node({**VALID, "reads": {"ids": sql}}, "f.yml")
    assert n.reads["ids"] == sql


def test_reads_allows_leading_block_comment():
    sql = "/* note */ select id from analytics.a"
    n = parse_node({**VALID, "reads": {"ids": sql}}, "f.yml")
    assert n.reads["ids"] == sql


def test_reads_unterminated_block_comment_treated_as_rest_is_comment():
    """An unterminated /* block comment consumes the rest of the string, so
    the shape check sees an empty statement and reports the normal
    "must start with SELECT or WITH" error (not a crash)."""
    sql = "/* not closed select id from analytics.a"
    with pytest.raises(ContractError, match="must start with SELECT or WITH"):
        parse_node({**VALID, "reads": {"ids": sql}}, "f.yml")


def test_reads_allows_leading_parenthesized_select():
    sql = "(select 1) union all (select 2)"
    n = parse_node({**VALID, "reads": {"ids": sql}}, "f.yml")
    assert n.reads["ids"] == sql


def test_reads_rejects_unterminated_single_quote():
    """An unterminated single-quoted string literal must not silently
    truncate the semicolon scan and let a multi-statement read through."""
    sql = "select 'unterminated from analytics.a; drop table x"
    with pytest.raises(ContractError, match="unterminated"):
        parse_node({**VALID, "reads": {"ids": sql}}, "f.yml")


def test_reads_rejects_unterminated_double_quote():
    sql = 'select "unterminated from analytics.a; drop table x'
    with pytest.raises(ContractError, match="unterminated"):
        parse_node({**VALID, "reads": {"ids": sql}}, "f.yml")


def test_reads_allows_properly_terminated_literal_with_doubled_quote_escape():
    sql = "select 'it''s fine' as tag from analytics.a"
    n = parse_node({**VALID, "reads": {"ids": sql}}, "f.yml")
    assert n.reads["ids"] == sql


def test_reads_still_rejects_drop_then_select():
    with pytest.raises(ContractError, match="reads.ids"):
        parse_node({**VALID, "reads": {"ids": "drop table x; select 1"}}, "f.yml")


def test_reads_still_rejects_two_selects():
    with pytest.raises(ContractError, match="reads.ids"):
        parse_node({**VALID, "reads": {"ids": "select 1; select 2"}}, "f.yml")


def test_output_column_unknown_key_rejected():
    cols = [{"name": "id", "type": "INTEGER", "nullabel": True}]
    with pytest.raises(ContractError, match="nullabel"):
        parse_node({**VALID, "output_columns": cols}, "f.yml")


def test_extra_columns_non_string_rejected():
    with pytest.raises(ContractError, match="extra_columns"):
        parse_node({**VALID, "extra_columns": ["raise"]}, "f.yml")


def test_load_dir_duplicate_yaml_key_rejected(tmp_path):
    a = tmp_path / "a.yml"
    a.write_text(
        "nodes:\n"
        "  - schema: analytics\n"
        "    table: t\n"
        "    owner: marketing\n"
        "    schedule: daily\n"
        "    criticality: SECONDARY\n"
        "    script: scripts/t.py\n"
        "    reads:\n"
        "      ids: select id from analytics.a\n"
        "      ids: select id from analytics.b\n"
        "    output_columns:\n"
        "      - {name: id, type: INTEGER}\n"
    )
    with pytest.raises(ContractError, match="duplicate"):
        load_contract_dir(tmp_path)


def test_load_dir_yaml_syntax_error_rejected(tmp_path):
    a = tmp_path / "a.yml"
    a.write_text("nodes: [unclosed\n  bad: : :\n")
    with pytest.raises(ContractError, match="a.yml"):
        load_contract_dir(tmp_path)


def test_load_dir_multi_document_rejected(tmp_path):
    a = tmp_path / "a.yml"
    a.write_text("nodes: []\n---\nnodes: []\n")
    with pytest.raises(ContractError, match="a.yml"):
        load_contract_dir(tmp_path)


def test_load_dir_duplicate_relation_across_files_rejected(tmp_path):
    a, b = tmp_path / "a.yml", tmp_path / "b.yml"
    a.write_text(yaml.safe_dump({"nodes": [VALID]}))
    b.write_text(yaml.safe_dump({"nodes": [VALID]}))
    with pytest.raises(ContractError, match="duplicate node analytics.t"):
        load_contract_dir(tmp_path)


def test_load_dir_happy_path(tmp_path):
    a = tmp_path / "a.yml"
    a.write_text(yaml.safe_dump({"nodes": [VALID]}))
    nodes = load_contract_dir(tmp_path)
    assert len(nodes) == 1
    assert nodes[0].relation == "analytics.t"


def test_load_dir_supports_yaml_extension(tmp_path):
    a = tmp_path / "a.yaml"
    a.write_text(yaml.safe_dump({"nodes": [VALID]}))
    nodes = load_contract_dir(tmp_path)
    assert len(nodes) == 1


def test_load_dir_empty_dir_rejected(tmp_path):
    with pytest.raises(ContractError, match="no contract files"):
        load_contract_dir(tmp_path)


def test_load_dir_no_nodes_rejected(tmp_path):
    a = tmp_path / "a.yml"
    a.write_text(yaml.safe_dump({"nodes": []}))
    with pytest.raises(ContractError, match="no contract files"):
        load_contract_dir(tmp_path)


def test_load_dir_non_mapping_document_rejected(tmp_path):
    a = tmp_path / "a.yml"
    a.write_text(yaml.safe_dump(["not", "a", "mapping"]))
    with pytest.raises(ContractError, match="a.yml"):
        load_contract_dir(tmp_path)


def test_load_dir_missing_nodes_key_rejected(tmp_path):
    a = tmp_path / "a.yml"
    a.write_text(yaml.safe_dump({"not_nodes": []}))
    with pytest.raises(ContractError, match="nodes"):
        load_contract_dir(tmp_path)


def test_load_dir_nodes_not_list_rejected(tmp_path):
    a = tmp_path / "a.yml"
    a.write_text(yaml.safe_dump({"nodes": "not-a-list"}))
    with pytest.raises(ContractError, match="nodes"):
        load_contract_dir(tmp_path)
