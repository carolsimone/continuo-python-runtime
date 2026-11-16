"""The schema rule for read queries: every table a read names must name its schema.

TOPOLOGY_CONTROLLER_CASES are the answers topology-controller's `referenced_tables` gives
for the same queries. CTE_SCOPE_CASES pin which names refer to a CTE: only one that is in
scope where the name is written.
"""

import pytest
from sqlglot.errors import ParseError

from continuo_python_runtime.contract.read_tables import unqualified_tables

DIALECTS = ["postgres", None]
# The dialects of the engines a service can run on, and sqlglot's neutral grammar.
ENGINE_DIALECTS = ["postgres", "trino", "duckdb", None]

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


# A name with no schema is a CTE only where a CTE of that name is in scope: the
# body of a non-recursive CTE does not see the CTE it defines, an earlier CTE is
# visible to a later one, and a CTE of a subquery is not visible outside it.
CTE_SCOPE_CASES = [
    pytest.param(
        "WITH orders AS (SELECT * FROM orders) SELECT * FROM orders",
        ["orders"],
        id="the-body-of-a-cte-reads-the-table-not-the-cte-it-defines",
    ),
    pytest.param(
        "WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM t WHERE n < 3) SELECT * FROM t",
        [],
        id="a-recursive-cte-reads-itself",
    ),
    pytest.param(
        "WITH a AS (SELECT 1 AS x), b AS (SELECT * FROM a) SELECT * FROM b",
        [],
        id="a-cte-reads-an-earlier-cte",
    ),
    pytest.param(
        "WITH a AS (SELECT * FROM b), b AS (SELECT 1 AS x) SELECT * FROM a",
        ["b"],
        id="a-cte-is-not-visible-before-it-is-defined",
    ),
    pytest.param(
        "SELECT * FROM (WITH users AS (SELECT 1 AS id) SELECT * FROM users) s JOIN users ON 1 = 1",
        ["users"],
        id="a-cte-of-a-subquery-is-not-visible-outside-it",
    ),
    pytest.param(
        "WITH users AS (SELECT 1 AS id) SELECT * FROM users WHERE id IN (SELECT id FROM users)",
        [],
        id="a-cte-is-visible-in-a-subquery-of-the-query-that-defines-it",
    ),
    pytest.param(
        "WITH Orders AS (SELECT 1 AS id) SELECT * FROM ORDERS",
        [],
        id="a-cte-reference-matches-the-cte-in-lower-case",
    ),
    pytest.param(
        "SELECT * FROM s.x, LATERAL (SELECT * FROM x) l",
        ["x"],
        id="a-qualified-table-does-not-make-the-same-unqualified-name-a-reference",
    ),
]


@pytest.mark.parametrize("dialect", ENGINE_DIALECTS)
@pytest.mark.parametrize(("sql", "tables"), CTE_SCOPE_CASES)
def test_a_name_is_a_cte_only_where_a_cte_of_that_name_is_in_scope(sql, tables, dialect):
    assert unqualified_tables(sql, dialect=dialect) == tables


@pytest.mark.parametrize("dialect", ["duckdb", None])
def test_a_pivoted_reference_to_a_cte_is_still_a_reference_to_it(dialect):
    sql = "WITH t AS (SELECT 1 AS a) SELECT * FROM t PIVOT(SUM(a) FOR a IN (1, 2))"
    assert unqualified_tables(sql, dialect=dialect) == []


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


@pytest.mark.parametrize("dialect", DIALECTS)
def test_sql_sqlglot_refuses_propagates_its_error(dialect):
    with pytest.raises(ParseError):
        unqualified_tables("SELECT FROM WHERE (", dialect=dialect)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_leading_empty_statement_propagates_sqlglots_error(dialect):
    """sqlglot's parse_one, which topology-controller uses too, refuses a query that starts with `;`."""
    with pytest.raises(ParseError):
        unqualified_tables("; SELECT * FROM raw.a", dialect=dialect)
