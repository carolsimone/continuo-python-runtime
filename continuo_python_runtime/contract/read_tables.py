"""The tables a read query names without their schema.

Which relation a table with no schema means depends on the session's search
path, so a read query must name the schema of every table it reads. The one
name that goes without a schema is a reference to a CTE that is in scope where
the name is used: the body of ``WITH orders AS (SELECT * FROM orders)`` reads
the table ``orders``, not the CTE it defines, while the CTEs of a
``WITH RECURSIVE`` list all see each other.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp


def unqualified_tables(sql: str, *, dialect: str | None) -> list[str]:
    """Return the lower-case names of the tables ``sql`` names with no schema.

    Each table is listed once, in the order sqlglot walks the query: the tables
    of the outer query come before those of its subqueries. A name with no
    schema is skipped only when it refers to a CTE that is in scope where the
    name is written. In a ``WITH`` list a CTE is in scope in the CTEs defined
    after it, in the query that carries the ``WITH`` clause and in the
    subqueries of those, and not in its own body. In a ``WITH RECURSIVE`` list
    every CTE is in scope in every CTE body of the list, itself and the CTEs
    defined after it included, and in the query that carries the clause and its
    subqueries. A CTE is never in scope in a query that contains the query that
    defines it. Any other name with no schema is a table, even when a CTE of
    that name is defined somewhere else in the query, and a qualified name is
    never a CTE reference and is never listed. A table with no schema and no
    name, which sqlglot models for a table function such as
    ``generate_series(1, 3)``, is listed by its SQL text under ``dialect``
    (lower case, without its alias). A table function that names its schema
    (``pg_catalog.generate_series(1, 3)``) reads no relation and is skipped.

    ``dialect`` is the sqlglot dialect the query is parsed under (``None`` is
    sqlglot's neutral grammar). A query sqlglot cannot parse raises sqlglot's
    own error: callers check that the query is one read first.
    """
    parsed = sqlglot.parse_one(sql, dialect=dialect)

    named: list[str] = []
    for table in parsed.find_all(exp.Table):
        if table.db:
            continue
        name = table.name.lower() or _sql_text(table, dialect)
        if (table.name and _names_a_visible_cte(table, name)) or name in named:
            continue
        named.append(name)
    return named


def _names_a_visible_cte(table: exp.Table, name: str) -> bool:
    """Whether the unqualified ``table`` refers to a CTE called ``name`` (lower case).

    Climbs from the table through its parents, so every kind of statement
    resolves the same way, one sqlglot cannot build a scope for (DuckDB's
    ``PIVOT`` statement) included. A query that carries a ``WITH`` clause shows
    every CTE of the clause to everything under it except the clause itself. A
    CTE body sees, in the clause it stands in, the CTEs defined before it, or
    all of them under ``WITH RECURSIVE``. Names compare in lower case.
    """
    child: exp.Expr = table
    node = table.parent
    while node is not None:
        visible: list[exp.Expr]
        if isinstance(node, exp.With):
            ctes = list(node.expressions)
            position = next((i for i, cte in enumerate(ctes) if cte is child), 0)
            visible = ctes if node.recursive else ctes[:position]
        else:
            clause = node.args.get("with_")
            visible = list(clause.expressions) if isinstance(clause, exp.With) and clause is not child else []
        if any(cte.alias.lower() == name for cte in visible):
            return True
        child, node = node, node.parent
    return False


def _sql_text(table: exp.Table, dialect: str | None) -> str:
    """The lower-case SQL of a table that has no name, without its alias."""
    bare = table.copy()
    bare.set("alias", None)
    return bare.sql(dialect=dialect).lower()
