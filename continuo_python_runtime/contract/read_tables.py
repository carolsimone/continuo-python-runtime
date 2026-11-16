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
from sqlglot.optimizer.scope import traverse_scope


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
    cte_references = _scoped_cte_references(parsed)

    named: list[str] = []
    for table in parsed.find_all(exp.Table):
        if table.db or id(table) in cte_references:
            continue
        name = table.name.lower() or _sql_text(table, dialect)
        if name in _recursive_cte_names(table) or name in named:
            continue
        named.append(name)
    return named


def _scoped_cte_references(parsed: exp.Expr) -> set[int]:
    """The ``id`` of every table of ``parsed`` that names a CTE in its sqlglot scope.

    A scope sees the CTEs defined before it, and a CTE sees itself only under
    ``WITH RECURSIVE``. Names compare in lower case. A table that no scope
    reaches is not in the result, so it counts as a table.
    """
    references: set[int] = set()
    for scope in traverse_scope(parsed):
        visible = {name.lower() for name in scope.cte_sources}
        references.update(
            id(table) for table in scope.tables if not table.db and table.name.lower() in visible
        )
    return references


def _recursive_cte_names(table: exp.Table) -> set[str]:
    """The lower-case names of the CTEs of every ``WITH RECURSIVE`` list that holds ``table``.

    ``table`` is held by a list when it stands in one of its CTE bodies, in the
    query that carries the list or in a subquery of either. A list that is not
    recursive adds nothing: sqlglot's scopes already resolve its names.
    """
    names: set[str] = set()
    node: exp.Expr | None = table.parent
    while node is not None:
        with_ = node.args.get("with_")
        if isinstance(with_, exp.With) and with_.recursive:
            names.update(cte.alias.lower() for cte in with_.expressions)
        node = node.parent
    return names


def _sql_text(table: exp.Table, dialect: str | None) -> str:
    """The lower-case SQL of a table that has no name, without its alias."""
    bare = table.copy()
    bare.set("alias", None)
    return bare.sql(dialect=dialect).lower()
