"""Script linting for forbidden imports, SQL literals, and data-access calls."""

import ast
import re
from pathlib import Path

# Forbidden warehouse driver modules (check root module of imports)
FORBIDDEN_DRIVERS = {
    "psycopg2",
    "sqlalchemy",
    "trino",
    "snowflake",
    "pyodbc",
    "duckdb",
    "sqlite3",
    "pymysql",
    "mysql",
    "clickhouse_driver",
    "pyhive",
}

# SQL pattern: detects select, insert, update, delete, create table statements
SQL_PATTERN = re.compile(
    r"(?is)\b(select\s.+?\sfrom\s|insert\s+into\s|update\s.+?\sset\s|delete\s+from\s|create\s+table\s)"
)

# Forbidden data-access method calls
FORBIDDEN_CALLS = {"read_sql", "read_sql_query", "read_sql_table", "execute", "read_database"}


def lint_source(source: str, filename: str) -> list[str]:
    """Lint a Python source code string for violations.

    Returns a list of violations in the format:
    "<filename>:<lineno>: <rule text>"

    Rules:
    - L1: forbidden warehouse driver import
    - L2: SQL string literal
    - L3: forbidden data-access call
    """
    violations = []

    # Try to parse the source code
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"{filename}:{e.lineno}: syntax error: {e.msg}"]

    # Walk the AST to find violations
    for node in ast.walk(tree):
        # L1: Check for forbidden imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_module = alias.name.split(".")[0]
                if root_module in FORBIDDEN_DRIVERS:
                    violations.append(
                        f"{filename}:{node.lineno}: forbidden warehouse driver import '{root_module}'"
                    )

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root_module = node.module.split(".")[0]
                if root_module in FORBIDDEN_DRIVERS:
                    violations.append(
                        f"{filename}:{node.lineno}: forbidden warehouse driver import '{root_module}'"
                    )

        # L2: Check for SQL string literals
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if SQL_PATTERN.search(node.value):
                # Include a snippet up to 40 characters
                snippet = node.value[:40]
                if len(node.value) > 40:
                    snippet += "..."
                violations.append(
                    f"{filename}:{node.lineno}: SQL string literal '{snippet}'"
                )

        # L3: Check for forbidden data-access calls
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                if node.func.attr in FORBIDDEN_CALLS:
                    violations.append(
                        f"{filename}:{node.lineno}: forbidden data-access call '{node.func.attr}'"
                    )

    return violations


def lint_paths(paths: list[Path]) -> list[str]:
    """Lint Python files in the given paths.

    For each path:
    - If it's a directory, recursively find all .py files (sorted)
    - If it's a file, lint it directly

    Returns aggregate violations in order.
    """
    violations = []

    # Collect all Python files to lint
    files_to_lint = []
    for path in paths:
        path = Path(path)
        if path.is_dir():
            # Recursively find all .py files, sorted
            py_files = sorted(path.rglob("*.py"))
            files_to_lint.extend(py_files)
        else:
            # Single file
            files_to_lint.append(path)

    # Lint each file
    for filepath in files_to_lint:
        source = filepath.read_text(encoding="utf-8")
        file_violations = lint_source(source, str(filepath))
        violations.extend(file_violations)

    return violations
