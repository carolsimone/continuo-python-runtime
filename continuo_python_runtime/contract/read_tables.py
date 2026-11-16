"""The tables a read query names without their schema.

Which relation a table with no schema means depends on the session's search
path, so a read query must name the schema of every table it reads. The one
name that goes without a schema is a reference to a CTE that is in scope where
the name is used, and sqlglot's scopes decide which CTEs those are: the body of
``WITH orders AS (SELECT * FROM orders)`` reads the table ``orders``, not the
CTE it defines.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope


def unqualified_tables(sql: str, *, dialect: str | None) -> list[str]:
    """Return the lower-case names of the tables ``sql`` names with no schema.

    Each table is listed once, in the order sqlglot walks the query: the tables
    of the outer query come before those of its subqueries. A name with no
    schema is skipped only when it refers to a CTE that is in scope where the
    name is written. A CTE is in scope in the CTEs defined after it, in the
    query that carries its ``WITH`` clause and in the subqueries of those, and
    in its own body only under ``WITH RECURSIVE``; it is not in scope in a query
    that contains the query that defines it. Any other name with no schema is a table, even when
    a CTE of that name is defined somewhere else in the query, and a qualified
    name is never a CTE reference and is never listed. A table with no schema
    and no name, which sqlglot models for a table function such as
    ``generate_series(1, 3)``, is listed by its SQL text under ``dialect``
    (lower case, without its alias). A table function that names its schema
    (``pg_catalog.generate_series(1, 3)``) reads no relation and is skipped.

    ``dialect`` is the sqlglot dialect the query is parsed under (``None`` is
    sqlglot's neutral grammar). A query sqlglot cannot parse raises sqlglot's
    own error: callers check that the query is one read first.
    """
    parsed = sqlglot.parse_one(sql, dialect=dialect)
    cte_references = _cte_references(parsed)

    named: list[str] = []
    for table in parsed.find_all(exp.Table):
        if table.db or id(table) in cte_references:
            continue
        name = table.name.lower() or _sql_text(table, dialect)
        if name in named:
            continue
        named.append(name)
    return named


def _cte_references(parsed: exp.Expr) -> set[int]:
    """The ``id`` of every table of ``parsed`` that refers to a CTE in scope.

    Names compare in lower case. A table that no scope reaches is not in the
    result, so it counts as a table.
    """
    references: set[int] = set()
    for scope in traverse_scope(parsed):
        visible = {name.lower() for name in scope.cte_sources}
        references.update(
            id(table) for table in scope.tables if not table.db and table.name.lower() in visible
        )
    return references


def _sql_text(table: exp.Table, dialect: str | None) -> str:
    """The lower-case SQL of a table that has no name, without its alias."""
    bare = table.copy()
    bare.set("alias", None)
    return bare.sql(dialect=dialect).lower()
