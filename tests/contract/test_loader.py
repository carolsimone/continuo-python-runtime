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


def test_config_non_json_serializable_value_rejected():
    with pytest.raises(ContractError, match="not JSON-serializable"):
        parse_node({**VALID, "config": {"foo": object()}}, "f.yml")


def test_load_dir_config_unquoted_yaml_date_rejected(tmp_path):
    """An unquoted date under 'config' (e.g. 'effective_date: 2026-08-07') is
    parsed by the YAML loader into a `datetime.date`, which is not a JSON
    scalar/list/dict. This is a realistic authoring mistake -- someone
    forgets to quote a date-shaped value -- and must be caught here rather
    than surfacing as an obscure serialization failure later in CI."""
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
        "    output_columns:\n"
        "      - {name: id, type: INTEGER}\n"
        "    config:\n"
        "      effective_date: 2026-08-07\n"
    )
    with pytest.raises(ContractError, match="not JSON-serializable"):
        load_contract_dir(tmp_path)


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


def test_reads_unterminated_block_comment_rejected():
    """An unterminated /* block comment fails sqlglot's tokenizer (a
    TokenError, not a ValueError -- ensure_single_read's own docstring
    guarantee that only ValueError escapes is not quite right for this
    input), so the loader must still turn it into a clean ContractError
    naming the read rather than letting the tokenizer error escape raw."""
    sql = "/* not closed select id from analytics.a"
    with pytest.raises(ContractError, match=r"reads\.ids.*Error tokenizing"):
        parse_node({**VALID, "reads": {"ids": sql}}, "f.yml")


def test_reads_allows_leading_parenthesized_select():
    sql = "(select 1) union all (select 2)"
    n = parse_node({**VALID, "reads": {"ids": sql}}, "f.yml")
    assert n.reads["ids"] == sql


def test_reads_rejects_unterminated_single_quote():
    """An unterminated single-quoted string literal must not silently let a
    multi-statement read through -- sqlglot's tokenizer fails on it (a
    TokenError), which the loader must still turn into a ContractError."""
    sql = "select 'unterminated from analytics.a; drop table x"
    with pytest.raises(ContractError, match=r"reads\.ids.*Error tokenizing"):
        parse_node({**VALID, "reads": {"ids": sql}}, "f.yml")


def test_reads_rejects_unterminated_double_quote():
    sql = 'select "unterminated from analytics.a; drop table x'
    with pytest.raises(ContractError, match=r"reads\.ids.*Error tokenizing"):
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


def test_load_dir_dialect_rejects_what_neutral_default_accepts(tmp_path):
    """Proves --dialect threads from load_contract_dir through parse_node to
    ensure_single_read, not just that the parameter exists: QUALIFY parses
    under sqlglot's dialect-neutral default but not under its trino dialect
    (verified against sqlglot 30.15.0, pinned transitively via
    continuo-validation-contract 0.6.0's own dependency)."""
    sql = "select a from analytics.a qualify row_number() over (order by a) = 1"
    node = {**VALID, "reads": {"ids": sql}}
    a = tmp_path / "a.yml"
    a.write_text(yaml.safe_dump({"nodes": [node]}))

    load_contract_dir(tmp_path)  # neutral default: accepted

    with pytest.raises(ContractError, match="reads.ids"):
        load_contract_dir(tmp_path, dialect="trino")


def test_load_dir_invalid_dialect_names_the_flag_not_the_read(tmp_path):
    """A typo'd --dialect value (e.g. the uppercased 'POSTGRES' instead of
    'postgres') must be reported as a bad flag, not confused with a rejected
    read: sqlglot.Dialect.get_or_raise raises a plain ValueError
    ("Unknown dialect 'POSTGRES'.") that is otherwise indistinguishable, at
    the per-read ensure_single_read catch site, from a read that genuinely
    fails to parse -- so dialect must be validated once, up front, before any
    read is even looked at. A perfectly valid read must not be blamed."""
    a = tmp_path / "a.yml"
    a.write_text(yaml.safe_dump({"nodes": [VALID]}))

    try:
        load_contract_dir(tmp_path, dialect="POSTGRES")
        pytest.fail("expected ContractError")
    except ContractError as exc:
        message = str(exc)
        assert "--dialect" in message
        assert "POSTGRES" in message
        # must not be mistaken for a read rejection
        assert "reads.ids" not in message
        assert "must be a single read query" not in message


def test_parse_node_dialect_reaches_ensure_single_read(monkeypatch):
    """A monkeypatched spy on ensure_single_read, guarding the wiring itself
    independent of any one dialect's grammar (see the QUALIFY test above for
    a real acceptance/rejection proof)."""
    calls = []

    def fake_ensure_single_read(sql, dialect=None):
        calls.append((sql, dialect))

    monkeypatch.setattr(
        "continuo_python_runtime.contract.loader.ensure_single_read",
        fake_ensure_single_read,
    )
    parse_node(VALID, "f.yml", dialect="postgres")
    assert calls == [(VALID["reads"]["ids"], "postgres")]


def test_parse_node_dialect_defaults_to_none(monkeypatch):
    calls = []

    def fake_ensure_single_read(sql, dialect=None):
        calls.append(dialect)

    monkeypatch.setattr(
        "continuo_python_runtime.contract.loader.ensure_single_read",
        fake_ensure_single_read,
    )
    parse_node(VALID, "f.yml")
    assert calls == [None]


# --- check_reads=False: the runtime's opt-out from the read-shape gate ---


def test_check_reads_false_skips_ensure_single_read(monkeypatch):
    """The one call the runtime opts out of; nothing else in parse_node moves."""
    calls = []

    def fake_ensure_single_read(sql, dialect=None):
        calls.append((sql, dialect))

    monkeypatch.setattr(
        "continuo_python_runtime.contract.loader.ensure_single_read",
        fake_ensure_single_read,
    )
    node = parse_node(VALID, "f.yml", check_reads=False)
    assert calls == []
    assert node.relation == "analytics.t"
    assert node.reads == VALID["reads"]


def test_check_reads_false_accepts_a_read_the_neutral_parser_rejects():
    """Ordinary postgres (`~`) that sqlglot's dialect-neutral grammar rejects."""
    unparseable_neutrally = {**VALID, "reads": {"ids": "select id from a where b ~ 'x'"}}
    with pytest.raises(ContractError):
        parse_node(unparseable_neutrally, "f.yml")
    assert parse_node(unparseable_neutrally, "f.yml", check_reads=False).relation


def test_check_reads_false_still_enforces_the_reads_map_shape():
    with pytest.raises(ContractError, match="non-empty mapping"):
        parse_node({**VALID, "reads": {}}, "f.yml", check_reads=False)
    with pytest.raises(ContractError, match="non-empty SQL string"):
        parse_node({**VALID, "reads": {"ids": ""}}, "f.yml", check_reads=False)
    with pytest.raises(ContractError, match="non-empty string"):
        parse_node({**VALID, "reads": {"": "select 1"}}, "f.yml", check_reads=False)


def test_check_reads_false_still_enforces_every_other_node_rule():
    for bad, match in [
        ({**VALID, "criticality": "WHENEVER"}, "criticality"),
        ({**VALID, "output_columns": []}, "output_columns"),
        ({**VALID, "output_columns": [{"name": "id", "type": "BLOB"}]}, "unsupported"),
        ({**VALID, "config": ["not", "a", "mapping"]}, "must be a mapping"),
        ({**VALID, "extra_columns": "shrug"}, "extra_columns"),
        ({**VALID, "nope": 1}, "unknown key"),
    ]:
        with pytest.raises(ContractError, match=match):
            parse_node(bad, "f.yml", check_reads=False)


def test_load_contract_dir_check_reads_false_still_rejects_duplicate_relations(tmp_path):
    for name in ("a.yml", "b.yml"):
        (tmp_path / name).write_text(yaml.safe_dump({"nodes": [VALID]}))
    with pytest.raises(ContractError, match="duplicate node"):
        load_contract_dir(tmp_path, check_reads=False)


def test_load_contract_dir_check_reads_false_still_validates_dialect(tmp_path):
    """A bad --dialect is still a bad flag even when reads are not parsed."""
    (tmp_path / "a.yml").write_text(yaml.safe_dump({"nodes": [VALID]}))
    with pytest.raises(ContractError, match="unknown --dialect"):
        load_contract_dir(tmp_path, dialect="POSTGRESQL-9", check_reads=False)


# --- cyclic `config` containers (P2-7) ---
#
# PyYAML's safe loader constructs genuinely recursive Python objects from
# anchors/aliases (`&c` / `*c`), so `_validate_config`'s recursive `_check`
# can be handed a container that contains itself. Without cycle detection
# that recurses until RecursionError, an uncaught traceback instead of the
# ContractError every other malformed `config` produces.


def test_load_dir_config_yaml_anchor_cycle_raises_contract_error_not_recursion_error(tmp_path):
    """A self-referential `config` (an anchored mapping whose own `indexes`
    list aliases it) must raise ContractError naming the circular reference,
    not propagate PyYAML's cyclic object as an uncaught RecursionError."""
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
        "    output_columns:\n"
        "      - {name: id, type: INTEGER}\n"
        "    config: &cfg\n"
        "      indexes:\n"
        "        - *cfg\n"
    )
    with pytest.raises(ContractError, match="circular"):
        load_contract_dir(a.parent)


def test_load_dir_config_yaml_anchor_dag_not_a_cycle_still_validates(tmp_path):
    """The same sub-mapping aliased twice at sibling positions is a legal
    DAG, not a cycle -- it must not be misreported as a circular reference."""
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
        "    output_columns:\n"
        "      - {name: id, type: INTEGER}\n"
        "    config:\n"
        "      a:\n"
        "        - &shared {columns: [id]}\n"
        "      b:\n"
        "        - *shared\n"
    )
    nodes = load_contract_dir(a.parent)
    assert nodes[0].config == {"a": [{"columns": ["id"]}], "b": [{"columns": ["id"]}]}


def test_load_contract_dir_threads_check_reads_to_parse_node(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        "continuo_python_runtime.contract.loader.ensure_single_read",
        lambda sql, dialect=None: calls.append(sql),
    )
    (tmp_path / "a.yml").write_text(yaml.safe_dump({"nodes": [VALID]}))
    load_contract_dir(tmp_path, check_reads=False)
    assert calls == []
    load_contract_dir(tmp_path)
    assert calls == [VALID["reads"]["ids"]]
