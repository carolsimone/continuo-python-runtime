"""Tests for script linting: forbidden imports, SQL literals, data-access calls."""

from continuo_python_runtime.lint import lint_source

GOOD = "def run(ctx):\n    ids = ctx.read('ids')\n    return ids\n"


def test_clean_script_passes():
    assert lint_source(GOOD, "s.py") == []


def test_driver_import_flagged():
    assert any("psycopg2" in v for v in lint_source("import psycopg2\n" + GOOD, "s.py"))
    assert any("sqlalchemy" in v for v in lint_source("from sqlalchemy import text\n" + GOOD, "s.py"))


def test_sql_literal_flagged():
    bad = GOOD + "q = 'select a from analytics.t'\n"
    assert any("SQL string literal" in v for v in lint_source(bad, "s.py"))


def test_read_sql_call_flagged():
    bad = GOOD + "def f(pd, c):\n    return pd.read_sql('x', c)\n"
    assert lint_source(bad, "s.py")


def test_violation_carries_location():
    (v,) = lint_source("import psycopg2\n", "scripts/x.py")
    assert v.startswith("scripts/x.py:1:")
