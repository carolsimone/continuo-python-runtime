"""The contract's single-read gate for :meth:`ValidationAdapter.check_binds`.

``check_binds`` accepts a single read query, raises on any bind failure, and
scans zero data. Enforcing "a single read" by wrapping the text in a subquery
and letting the engine's parser object is not enough: a read that closes the
wrap's own parenthesis and reopens it after an injected statement leaves the
wrap balanced, and an engine whose driver batches ``;``-separated statements
(psycopg2's simple-query protocol does) then executes the injected statement
for real. So the shape is decided here, by parsing, before any text reaches an
engine.

This lives in the contract rather than in each adapter because the rule is
engine-independent and every adapter — including third-party ones — needs the
same one.
"""
import logging

import sqlglot

from sqlglot import exp

logger = logging.getLogger(__name__)

# The expression types a legitimate read parses to. Validated against the full
# set of read shapes a spec may declare (plain/star selects, WHERE/ORDER
# BY/LIMIT, CTEs, UNION ALL, GROUP BY, joins, FROM-less selects, VALUES,
# duplicate output names, trailing semicolons, trailing line comments). Narrow
# it further and legitimate reads start getting rejected.
_READ_TYPES = (exp.Select, exp.Union, exp.Values, exp.Subquery)

# Rejection messages quote the offending read, and ride back to the controller
# inside the runner's result block — so bound every variable-length part of
# them, both the read itself and any parser detail appended to it.
_MAX_EXCERPT = 200


def _excerpt(text: str) -> str:
    """Render *text* for an error message: whitespace collapsed, length bounded."""
    collapsed = " ".join(text.split())
    if len(collapsed) > _MAX_EXCERPT:
        return f"{collapsed[:_MAX_EXCERPT]}... (truncated)"
    return collapsed


def ensure_single_read(sql: str, dialect: str | None = None) -> None:
    """Raise ValueError unless *sql* is exactly one read query.

    Parameters
    ----------
    sql
        The declared read, as written in the spec.
    dialect
        A sqlglot dialect name (e.g. ``"postgres"``, ``"trino"``). Adapters
        pass their engine's dialect; ``None`` parses with sqlglot's default.

    Raises
    ------
    ValueError
        If *sql* does not parse, parses to anything other than exactly one
        statement, or parses to a statement that is not a query. Every
        rejection is a ValueError — a sqlglot ``ParseError`` never escapes, so
        callers need not import sqlglot to handle a rejected read.
    """
    try:
        parsed = sqlglot.parse(sql, dialect=dialect)
    except sqlglot.ParseError as exc:
        raise ValueError(
            f"check_binds accepts a single read query, and this read does not parse "
            f"as {dialect or 'SQL'}: {_excerpt(sql)} [{_excerpt(str(exc))}]"
        ) from exc

    statements = [statement for statement in parsed if statement is not None]
    if len(statements) != 1:
        raise ValueError(
            f"check_binds accepts exactly one read query, but this read parses as "
            f"{len(statements)} statements: {_excerpt(sql)}"
        )

    statement = statements[0]
    if not isinstance(statement, _READ_TYPES):
        raise ValueError(
            f"check_binds accepts a single read query, but this read is a "
            f"{statement.key.upper()} statement: {_excerpt(sql)}"
        )

    logger.debug("read accepted as a single query by the %s gate", dialect or "default")
