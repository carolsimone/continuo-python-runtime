"""The schema rule for read queries: every table a read names must name its schema.

The cases mirror topology-controller's `referenced_tables`, which refuses the same
queries when a release is loaded, so producer CI and the release agree.
"""

import pytest
from sqlglot.errors import ParseError

from continuo_python_runtime.contract.read_tables import unqualified_tables

DIALECTS = ["postgres", None]

# (case id, the read queries, the answer for each query). The answer is what
# topology-controller does with the same query: a query it refuses lists the
# table it names without a schema, a query it accepts lists nothing.
TOPOLOGY_CONTROLLER_CASES = [
    pytest.param(
        ("WITH x AS (SELECT * FROM raw.a) SELECT * FROM x JOIN analytics.m ON 1=1",),
        ([],),
        id="ctes-and-qualified-self-reads-are-skipped",
    ),
    pytest.param(
        ("WITH users AS (SELECT 1 AS id) SELECT * FROM public.users JOIN users ON 1=1",),
        ([],),
        id="a-qualified-table-is-kept-when-a-cte-has-its-name",
    ),
    pytest.param(
        ("WITH users AS (SELECT 1 AS id) SELECT * FROM users JOIN public.users ON 1=1",),
        ([],),
        id="a-qualified-table-that-shares-a-ctes-name-is-kept-whichever-is-read-first",
    ),
    pytest.param(
        ("WITH users AS (SELECT 1 AS id) SELECT * FROM users",),
        ([],),
        id="an-unqualified-reference-to-a-cte-is-skipped",
    ),
    pytest.param(
        ("WITH users AS (SELECT 1) SELECT * FROM users JOIN orders ON 1=1",),
        (["orders"],),
        id="an-unqualified-table-that-is-not-a-cte-is-refused-beside-a-cte-of-another-name",
    ),
    pytest.param(
        ("WITH users AS (SELECT 1) SELECT * FROM users", "SELECT * FROM users"),
        ([], ["users"]),
        id="a-cte-in-one-read-does-not-hide-the-same-name-in-another",
    ),
    pytest.param(
        ("SELECT * FROM generate_series(1, 3)",),
        (["generate_series(1, 3)"],),
        id="a-nameless-table-with-no-schema-is-refused-by-its-sql-text",
    ),
    pytest.param(
        ("SELECT * FROM (VALUES (1)) AS v(x)",),
        ([],),
        id="values-names-no-table",
    ),
    pytest.param(
        ("SELECT * FROM unnest(ARRAY[1,2]) AS u(x)",),
        ([],),
        id="unnest-names-no-table",
    ),
    pytest.param(
        ("SELECT * FROM public.my_fn(1)",),
        ([],),
        id="a-schema-qualified-function-call-names-no-table",
    ),
    pytest.param(
        ("SELECT * FROM pg_catalog.generate_series(1, 3)",),
        ([],),
        id="a-schema-qualified-generate-series-names-no-table",
    ),
]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(("queries", "answers"), TOPOLOGY_CONTROLLER_CASES)
def test_the_schema_rule_matches_topology_controller(queries, answers, dialect):
    assert [unqualified_tables(sql, dialect=dialect) for sql in queries] == list(answers)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_table_comes_back_lower_case_in_first_seen_order_once_each(dialect):
    sql = "SELECT * FROM Orders AS o JOIN \"Customers\" c ON 1=1 JOIN ORDERS o2 ON 1=1"
    assert unqualified_tables(sql, dialect=dialect) == ["orders", "customers"]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_table_in_a_subquery_or_a_join_is_found(dialect):
    sql = "SELECT * FROM raw.a WHERE id IN (SELECT id FROM b) AND EXISTS (SELECT 1 FROM c)"
    assert sorted(unqualified_tables(sql, dialect=dialect)) == ["b", "c"]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_tables_of_the_outer_query_come_before_those_of_its_subqueries(dialect):
    """The walk is sqlglot's breadth-first one, the order topology-controller reports in."""
    sql = "SELECT * FROM (SELECT * FROM inner_t) s JOIN outer_t ON 1=1"
    assert unqualified_tables(sql, dialect=dialect) == ["outer_t", "inner_t"]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_query_that_names_only_qualified_tables_has_none(dialect):
    sql = 'SELECT * FROM raw.a JOIN "wh"."raw"."b" ON 1=1'
    assert unqualified_tables(sql, dialect=dialect) == []


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_nameless_table_is_named_without_its_alias(dialect):
    sql = "SELECT * FROM generate_series(1, 3) AS g(x)"
    assert unqualified_tables(sql, dialect=dialect) == ["generate_series(1, 3)"]


def test_a_nameless_table_is_rendered_under_the_given_dialect():
    sql = "SELECT * FROM generate_series(1, 3)"
    assert unqualified_tables(sql, dialect="trino") == ["unnest(sequence(1, 3))"]


def test_a_nameless_table_is_listed_once():
    sql = "SELECT * FROM generate_series(1, 3) a JOIN generate_series(1, 3) b ON 1=1"
    assert unqualified_tables(sql, dialect="postgres") == ["generate_series(1, 3)"]


def test_sql_sqlglot_refuses_propagates_its_error():
    with pytest.raises(ParseError):
        unqualified_tables("SELECT FROM WHERE (", dialect="postgres")
