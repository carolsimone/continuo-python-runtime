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


def test_concat_with_nonliteral_constant_flagged():
    """SQL constant concatenated with nonliteral should be flagged via constant."""
    source = GOOD + "q = 'select id from analytics.t ' + suffix\n"
    violations = lint_source(source, "s.py")
    # The constant part should still be flagged even though reconstruction fails
    assert any("SQL string literal" in v for v in violations)


def test_fstring_concat_with_nonliteral_flagged():
    """F-string SQL concatenated with nonliteral should be flagged via f-string."""
    source = GOOD + "q = f'select {c} from t' + suffix\n"
    violations = lint_source(source, "s.py")
    # The f-string part should be flagged
    assert any("SQL string literal" in v for v in violations)


def test_private_attribute_access_flagged():
    bad = GOOD + "def f(ctx):\n    return ctx._fetch('x')\n"
    violations = lint_source(bad, "s.py")
    assert any("private attribute access '_fetch'" in v for v in violations)


def test_dunder_attribute_access_flagged():
    bad = GOOD + "def f(x):\n    return x.__dict__\n"
    violations = lint_source(bad, "s.py")
    assert any("private attribute access '__dict__'" in v for v in violations)


def test_normal_read_call_clean():
    assert lint_source(GOOD, "s.py") == []


def test_name_dunder_name_comparison_not_flagged():
    source = GOOD + "if __name__ == '__main__':\n    pass\n"
    violations = lint_source(source, "s.py")
    assert not any("private attribute access" in v for v in violations)


def test_full_concat_no_double_report():
    """Fully-literal concat should not double-report."""
    source = GOOD + "q = 'select id ' + 'from t'\n"
    violations = lint_source(source, "s.py")
    # Should have exactly one violation (from the BinOp reconstruction)
    sql_violations = [v for v in violations if "SQL string literal" in v]
    assert len(sql_violations) == 1
