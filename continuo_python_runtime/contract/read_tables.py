"""The tables a read query names without their schema.

Which relation a table with no schema means depends on the session's search
path, so a read query must name the schema of every table it reads. A CTE is
the one table a query names without one. topology-controller applies the same
rule to the read queries of a release, so a contract that passes here passes
there.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp


def unqualified_tables(sql: str, *, dialect: str | None) -> list[str]:
    """Return the lower-case names of the tables ``sql`` names with no schema.

    Each table is listed once, in the order sqlglot walks the query: the tables
    of the outer query come before those of its subqueries, the order
    topology-controller reports them in. A name with no schema that is a CTE of
    the same query is a reference to that CTE, not a table, so it is skipped; a
    qualified name is a table even when a CTE of the same name is in scope, and
    a qualified table is never listed. A table with no schema and no name, which
    sqlglot models for a table function such as ``generate_series(1, 3)``, is
    listed by its SQL text under ``dialect`` (lower case, without its alias). A
    table function that names its schema (``pg_catalog.generate_series(1, 3)``)
    reads no relation and is skipped.

    ``dialect`` is the sqlglot dialect the query is parsed under (``None`` is
    sqlglot's neutral grammar). A query sqlglot cannot parse raises sqlglot's
    own error: callers check that the query is one read first.
    """
    parsed = sqlglot.parse_one(sql, dialect=dialect)
    cte_names = {cte.alias.lower() for cte in parsed.find_all(exp.CTE)}

    named: list[str] = []
    for table in parsed.find_all(exp.Table):
        if table.db:
            continue
        name = table.name.lower() or _sql_text(table, dialect)
        if name in cte_names or name in named:
            continue
        named.append(name)
    return named


def _sql_text(table: exp.Table, dialect: str | None) -> str:
    """The lower-case SQL of a table that has no name, without its alias."""
    bare = table.copy()
    bare.set("alias", None)
    return bare.sql(dialect=dialect).lower()
