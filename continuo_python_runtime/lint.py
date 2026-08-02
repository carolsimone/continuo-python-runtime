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


def _reconstruct_joined_str(node: ast.JoinedStr) -> str:
    """Reconstruct text from a JoinedStr (f-string) by joining Constant parts with space placeholders."""
    parts = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        else:
            # FormattedValue or other node type - use space placeholder
            parts.append(" ")
    return "".join(parts)


def _reconstruct_binop_concat(node: ast.BinOp) -> str | None:
    """Reconstruct string from BinOp(Add) with string Constant/JoinedStr leaves."""
    if not isinstance(node.op, ast.Add):
        return None

    # Collect all parts of the binary operation tree
    def collect_parts(n: ast.expr) -> list[str] | None:
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            return [n.value]
        elif isinstance(n, ast.JoinedStr):
            return [_reconstruct_joined_str(n)]
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            left = collect_parts(n.left)
            right = collect_parts(n.right)
            if left is not None and right is not None:
                return left + right
            return None
        else:
            return None

    parts = collect_parts(node)
    return "".join(parts) if parts is not None else None


def lint_source(source: str, filename: str) -> list[str]:
    """Lint a Python source code string for violations.

    Returns a list of violations in the format:
    "<filename>:<lineno>: <rule text>"

    Rules:
    - L1: forbidden warehouse driver import
    - L2: SQL string literal (including in f-strings and concatenated strings)
    - L3: forbidden data-access call (attribute or imported function)
    """
    violations = []

    # Try to parse the source code
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"{filename}:{e.lineno}: syntax error: {e.msg}"]

    # Track imported data-access functions and their local aliases
    imported_forbidden_calls: dict[str, int] = {}  # {local_name: lineno}

    # First pass: collect information about imports
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

            # Track imports of forbidden data-access functions
            if node.names:
                for alias in node.names:
                    # alias.name is the original name, alias.asname is the local alias (or None)
                    if alias.name in FORBIDDEN_CALLS:
                        local_name = alias.asname if alias.asname else alias.name
                        imported_forbidden_calls[local_name] = node.lineno
                        violations.append(
                            f"{filename}:{node.lineno}: forbidden data-access import '{alias.name}'"
                        )

    # Track constants consumed in compound expressions (to avoid double-reporting)
    consumed_constants: set[int] = set()

    # Second pass: check for SQL and L3 violations
    for node in ast.walk(tree):
        # L2: Check for SQL in JoinedStr (f-strings)
        if isinstance(node, ast.JoinedStr):
            reconstructed = _reconstruct_joined_str(node)
            if SQL_PATTERN.search(reconstructed):
                snippet = reconstructed[:40]
                if len(reconstructed) > 40:
                    snippet += "..."
                violations.append(
                    f"{filename}:{node.lineno}: SQL string literal '{snippet}'"
                )
            # Mark constants in this JoinedStr as consumed
            for value in node.values:
                if isinstance(value, ast.Constant):
                    consumed_constants.add(id(value))

        # L2: Check for SQL in BinOp concatenation
        elif isinstance(node, ast.BinOp):
            reconstructed_text = _reconstruct_binop_concat(node)
            if reconstructed_text is not None and SQL_PATTERN.search(reconstructed_text):
                snippet = reconstructed_text[:40]
                if len(reconstructed_text) > 40:
                    snippet += "..."
                violations.append(
                    f"{filename}:{node.lineno}: SQL string literal '{snippet}'"
                )
            # Mark constants in this BinOp as consumed
            for n in ast.walk(node):
                if isinstance(n, ast.Constant):
                    consumed_constants.add(id(n))

        # L2: Check for SQL in plain string literals (skip if consumed by compound)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in consumed_constants:
                if SQL_PATTERN.search(node.value):
                    snippet = node.value[:40]
                    if len(node.value) > 40:
                        snippet += "..."
                    violations.append(
                        f"{filename}:{node.lineno}: SQL string literal '{snippet}'"
                    )

        # L3: Check for forbidden data-access calls
        elif isinstance(node, ast.Call):
            # Attribute access: pd.read_sql(...)
            if isinstance(node.func, ast.Attribute):
                if node.func.attr in FORBIDDEN_CALLS:
                    violations.append(
                        f"{filename}:{node.lineno}: forbidden data-access call '{node.func.attr}'"
                    )

            # Name reference: read_sql_table(...) or rs(...) where rs is an alias
            elif isinstance(node.func, ast.Name):
                if node.func.id in imported_forbidden_calls:
                    violations.append(
                        f"{filename}:{node.lineno}: forbidden data-access call '{node.func.id}'"
                    )

    return violations


def lint_paths(paths: list[Path]) -> list[str]:
    """Lint Python files in the given paths.

    For each path:
    - If it's a directory, recursively find all .py files (sorted)
    - If it's a file, lint it directly
    - If path does not exist, record a violation instead of crashing

    Returns aggregate violations in order.
    """
    violations = []

    # Collect all Python files to lint
    files_to_lint = []
    for path in paths:
        path = Path(path)
        if not path.exists():
            violations.append(f"{path}: path does not exist")
        elif path.is_dir():
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
