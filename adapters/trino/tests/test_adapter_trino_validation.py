"""Unit tests for the trino adapter — mock-free.

DDL behavior (ensure_schema, drop_schema, build_empty_from_sql, clone_empty_from_prod,
the connection built by from_env, and DDL validity) is verified against a live Trino +
Iceberg stack in test_integration_trino.py, not with mocked cursors/connections here.
"""
import pytest

from continuo_python_runtime_trino.adapter import (
    TrinoAdapter,
    _quote,
    _sql_string,
    _table_properties,
    _trino_type,
)


def test_required_env_names_host_and_catalog():
    """Test that required_env lists exactly the two mandatory TRINO_* vars."""
    assert TrinoAdapter.required_env() == ["TRINO_HOST", "TRINO_CATALOG"]


def test_quote_rejects_empty_identifier() -> None:
    """Test that an empty identifier raises instead of quoting to an empty pair of quotes."""
    with pytest.raises(ValueError):
        _quote("")


def test_build_empty_from_columns_rejects_bad_type_before_touching_db() -> None:
    """Test that a malformed type string is rejected before any DB access."""
    adapter = TrinoAdapter.__new__(TrinoAdapter)  # no connection needed
    with pytest.raises(ValueError):
        adapter.build_empty_from_columns(
            "s", "t", [{"name": "id", "type": "INTEGER; DROP TABLE x"}], {}
        )


@pytest.mark.parametrize(
    ("type_str", "expected"),
    [
        ("TEXT", "VARCHAR"),
        ("text", "VARCHAR"),
        ("DOUBLE PRECISION", "DOUBLE"),
        ("double precision", "DOUBLE"),
        ("NUMERIC(10,2)", "DECIMAL(10,2)"),
        ("numeric(10,2)", "DECIMAL(10,2)"),
        ("BIGINT", "BIGINT"),
        ("VARCHAR(255)", "VARCHAR(255)"),
    ],
)
def test_trino_type_maps_grammar_spellings_to_trino_ddl(type_str: str, expected: str) -> None:
    """Test that each contract-grammar spelling maps to its Trino DDL spelling."""
    assert _trino_type(type_str) == expected


def test_empty_config_renders_no_with_clause():
    """Back-compat pin: no config means the bare 0.4.0 CREATE TABLE, byte for byte."""
    assert _table_properties({}) == ""


def test_properties_render_in_a_fixed_order_regardless_of_input_order():
    """DDL must be deterministic: spec JSON key order must not change the statement."""
    forward = _table_properties(
        {"partitioning": ["ts"], "sorted_by": ["id"], "format": "PARQUET", "format_version": 2}
    )
    reversed_ = _table_properties(
        {"format_version": 2, "format": "PARQUET", "sorted_by": ["id"], "partitioning": ["ts"]}
    )
    assert forward == reversed_
    assert forward == (
        " WITH (partitioning = ARRAY['ts'], sorted_by = ARRAY['id'], "
        "format = 'PARQUET', format_version = 2)"
    )


def test_only_the_declared_keys_are_emitted():
    """A partial config emits only what it declared — no defaults invented here."""
    assert _table_properties({"sorted_by": ["id"]}) == " WITH (sorted_by = ARRAY['id'])"


def test_unknown_config_key_is_rejected():
    """A key outside the trino vocabulary is an authoring error, never ignored."""
    with pytest.raises(ValueError, match="sortkey"):
        _table_properties({"sortkey": ["id"]})


def test_location_is_not_in_the_vocabulary():
    """An explicit location would aim the candidate build at the prod data path."""
    with pytest.raises(ValueError, match="location"):
        _table_properties({"location": "s3://warehouse/prod/orders"})


def test_sql_string_doubles_embedded_quotes():
    """Property values are escaped, not trusted — they carry author input into DDL."""
    assert _sql_string("a'b") == "'a''b'"
    assert _sql_string("x') ; DROP TABLE t --") == "'x'') ; DROP TABLE t --'"


@pytest.mark.parametrize("key", ["partitioning", "sorted_by"])
@pytest.mark.parametrize(
    "value", [[], "id", ["id", ""], ["id", 3], None], ids=["empty", "str", "blank", "int", "null"]
)
def test_array_properties_reject_anything_but_a_non_empty_string_list(key, value):
    """A malformed array must raise rather than render a broken or partial clause."""
    with pytest.raises(ValueError, match=key):
        _table_properties({key: value})


@pytest.mark.parametrize("value", ["", 2, None], ids=["empty", "int", "null"])
def test_format_must_be_one_of_the_allowed_values(value):
    """Format is a string literal; a number would render as an unquoted token."""
    with pytest.raises(ValueError, match="format"):
        _table_properties({"format": value})


@pytest.mark.parametrize("value", [True, "2", 2.0, None], ids=["bool", "str", "float", "null"])
def test_format_version_must_be_a_plain_integer(value):
    """Bool is an int subclass in python: `True` would silently render as `1`."""
    with pytest.raises(ValueError, match="format_version"):
        _table_properties({"format_version": value})
