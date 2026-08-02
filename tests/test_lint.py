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


def test_split_sql_in_fstring_flagged():
    """SQL split across f-string parts should be detected."""
    bad = GOOD + "q = f'select {column} from analytics.t'\n"
    assert any("SQL string literal" in v for v in lint_source(bad, "s.py"))


def test_concatenated_sql_flagged():
    """SQL concatenated with + operator should be detected."""
    bad = GOOD + "q = 'select a ' + 'from analytics.t'\n"
    assert any("SQL string literal" in v for v in lint_source(bad, "s.py"))


def test_benign_fstring_not_flagged():
    """F-strings without SQL should not be flagged."""
    source = GOOD + "msg = f'hello {name}'\n"
    assert lint_source(source, "s.py") == []


def test_from_import_read_sql_table_flagged():
    """Importing forbidden data-access function should be flagged."""
    source = "from pandas import read_sql_table\n" + GOOD
    violations = lint_source(source, "s.py")
    assert any("forbidden data-access import" in v for v in violations)


def test_imported_read_sql_call_flagged():
    """Calling imported data-access function should be flagged."""
    source = "from pandas import read_sql_table\n" + GOOD + "def f():\n    return read_sql_table()\n"
    violations = lint_source(source, "s.py")
    assert any("forbidden data-access call" in v for v in violations)


def test_aliased_import_flagged():
    """Aliased import of forbidden function should be flagged."""
    source = "from pandas import read_sql as rs\n" + GOOD + "def f():\n    return rs()\n"
    violations = lint_source(source, "s.py")
    # Should flag both the import and the call
    assert len(violations) >= 2
    assert any("forbidden data-access import" in v for v in violations)
    assert any("forbidden data-access call" in v for v in violations)
