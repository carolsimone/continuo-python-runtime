"""Unit tests for the single-read gate every adapter runs before EXPLAIN."""
import pytest

from continuo_engine_contract.sql import ensure_single_read

DIALECTS = ["postgres", "trino", None]

# The read shapes a legitimate spec is allowed to declare. Every one of these
# must survive the gate on every dialect — this list is the regression surface.
LEGIT_READS = [
    "select a, b from s.up",
    "select * from s.up",
    "select a from s.up where b = 'x;y' order by a limit 10",
    "with c as (select a from s.up) select a from c",
    "select a from s.up union all select a from s.up2",
    "select a, count(*) from s.up group by a",
    "select l.a from s.up l join s.up2 r on l.a = r.a",
    "select 1",
    "VALUES (1), (2)",
    "select a as x, b as x from s.up",
    "select a from s.up;",
    "select a from s.up -- trailing comment",
]

# The paren-balanced escapes: the wrap alone lets these through, because the
# read closes the wrap's own paren and reopens it after the injected statement.
PAREN_ESCAPE_DELETE = "select 1) AS x; DELETE FROM s.target; SELECT * FROM (SELECT 1"
PAREN_ESCAPE_DROP = "select 1) AS x; DROP TABLE s.target; SELECT * FROM (SELECT 1"

STACKED = "select a from s.up; delete from s.up"

BARE_DML = [
    "delete from s.up",
    "insert into s.up values (1)",
    "update s.up set a = 1",
    "drop table s.up",
    "create table s.x (a int)",
    "truncate table s.up",
]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("sql", LEGIT_READS)
def test_legitimate_reads_pass(sql: str, dialect: str | None) -> None:
    """Every legitimate read shape must survive the gate unchanged."""
    ensure_single_read(sql, dialect=dialect)  # must not raise


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("sql", [PAREN_ESCAPE_DELETE, PAREN_ESCAPE_DROP])
def test_paren_balanced_escape_is_rejected(sql: str, dialect: str | None) -> None:
    """A read that balances the subquery wrap's parens must not reach the engine."""
    with pytest.raises(ValueError):
        ensure_single_read(sql, dialect=dialect)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_stacked_statement_is_rejected(dialect: str | None) -> None:
    """A naively stacked second statement is rejected as more than one statement."""
    with pytest.raises(ValueError, match="exactly one"):
        ensure_single_read(STACKED, dialect=dialect)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("sql", BARE_DML)
def test_bare_non_query_statements_are_rejected(sql: str, dialect: str | None) -> None:
    """A single statement that is not a read is rejected as not a query."""
    with pytest.raises(ValueError, match="read query"):
        ensure_single_read(sql, dialect=dialect)


@pytest.mark.parametrize("sql", [PAREN_ESCAPE_DELETE, STACKED, "delete from s.up"])
def test_rejection_message_names_the_offending_read(sql: str) -> None:
    """Every rejection must be actionable: it quotes the read that was rejected."""
    with pytest.raises(ValueError) as excinfo:
        ensure_single_read(sql, dialect="postgres")
    message = str(excinfo.value)
    assert "read" in message
    assert sql[:30] in message


def test_unparseable_read_is_rejected_as_valueerror_not_a_sqlglot_error() -> None:
    """Callers catch ValueError; a sqlglot ParseError must never escape the gate."""
    with pytest.raises(ValueError):
        ensure_single_read("select from from where", dialect="postgres")


def test_empty_read_is_rejected() -> None:
    """An empty read parses to zero statements, which is not exactly one."""
    with pytest.raises(ValueError, match="exactly one"):
        ensure_single_read("   ", dialect="postgres")


@pytest.mark.parametrize("sql", [
    "delete from s.up where a in (" + ", ".join(str(i) for i in range(500)) + ")",
    "select from from where " + ", ".join(str(i) for i in range(500)),  # ParseError path
    "select a from s.up; " * 200,  # many-statements path
])
def test_long_read_is_truncated_in_the_message(sql: str) -> None:
    """Every rejection message is bounded: it rides the result block to the controller."""
    with pytest.raises(ValueError) as excinfo:
        ensure_single_read(sql, dialect="postgres")
    assert len(str(excinfo.value)) < 600
