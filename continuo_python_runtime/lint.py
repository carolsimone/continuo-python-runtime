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


def _reconstruct_joined_str(node: ast.JoinedStr) -> tuple[str, set[int]]:
    """Reconstruct text from a JoinedStr (f-string) by joining Constant parts
    with space placeholders.

    Returns the reconstructed text together with the ids of the Constant
    nodes that were actually folded into it (the JoinedStr's direct literal
    parts). Constants nested inside a ``FormattedValue`` expression (e.g.
    ``f'{"select ... from ..."}'``) are NEVER consumed here — they are
    replaced with a space placeholder and remain visible to the plain-Constant
    pass in ``lint_source``.
    """
    parts = []
    consumed: set[int] = set()
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
            consumed.add(id(value))
        else:
            # FormattedValue or other node type - use space placeholder.
            # Its contents (including any nested Constants) are intentionally
            # left unconsumed.
            parts.append(" ")
    return "".join(parts), consumed


def _reconstruct_binop_concat(node: ast.BinOp) -> tuple[str, set[int]] | None:
    """Reconstruct string from BinOp(Add) with string Constant/JoinedStr leaves.

    Returns the reconstructed text together with the ids of the Constant
    nodes actually folded into it: leaf string Constants and the literal
    fragments of any JoinedStr operand (including nested JoinedStr/BinOp
    fragments). Constants that live inside a FormattedValue expression are
    never included, matching ``_reconstruct_joined_str``.
    """
    if not isinstance(node.op, ast.Add):
        return None

    # Collect all parts of the binary operation tree
    def collect_parts(n: ast.expr) -> tuple[list[str], set[int]] | None:
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            return [n.value], {id(n)}
        elif isinstance(n, ast.JoinedStr):
            text, consumed = _reconstruct_joined_str(n)
            return [text], consumed
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            left = collect_parts(n.left)
            right = collect_parts(n.right)
            if left is not None and right is not None:
                return left[0] + right[0], left[1] | right[1]
            return None
        else:
            return None

    result = collect_parts(node)
    if result is None:
        return None
    parts, consumed = result
    return "".join(parts), consumed


def _docstring_constant_ids(tree: ast.Module) -> set[int]:
    """Return ids of Constant nodes that are docstrings.

    A str Constant is a docstring if it is the value of an ``ast.Expr``
    statement appearing as the FIRST statement of a Module/ClassDef/
    FunctionDef/AsyncFunctionDef body.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def lint_source(source: str, filename: str) -> list[str]:
    """Lint a Python source code string for violations.

    Returns a list of violations in the format:
    "<filename>:<lineno>: <rule text>"

    Rules:
    - L1: forbidden warehouse driver import
    - L2: SQL string literal (including in f-strings and concatenated strings)
    - L3: forbidden data-access call (attribute or imported function)
    - L4: private/protected attribute access (any ``ast.Attribute`` whose
      ``attr`` starts with ``_``), closing bypasses like ``ctx._fetch``.
      ``__name__``/``__main__`` are ``ast.Name`` nodes, not attributes, so
      they are never matched and need no exemption. Exempted: attribute
      access on ``self``/``cls`` (e.g. ``self._helper()``), so a script's own
      class-private helpers aren't flagged.

    Docstrings (the first statement of a Module/ClassDef/FunctionDef/
    AsyncFunctionDef body, when it is a bare string Expr) are exempt from the
    L2 constant pass, since prose commonly contains words like "select" and
    "from". This is a best-effort, position-based exemption: the same prose
    assigned to a variable is still flagged.
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

    # Docstrings are exempt from the L2 constant pass (prose false positives)
    docstring_constants = _docstring_constant_ids(tree)

    # Second pass: check for SQL and L3 violations
    for node in ast.walk(tree):
        # L2: Check for SQL in JoinedStr (f-strings)
        if isinstance(node, ast.JoinedStr):
            reconstructed, consumed = _reconstruct_joined_str(node)
            if SQL_PATTERN.search(reconstructed):
                snippet = reconstructed[:40]
                if len(reconstructed) > 40:
                    snippet += "..."
                violations.append(
                    f"{filename}:{node.lineno}: SQL string literal '{snippet}'"
                )
            # Only the literal fragments actually folded into the
            # reconstruction are consumed; FormattedValue contents are not.
            consumed_constants |= consumed

        # L2: Check for SQL in BinOp concatenation
        elif isinstance(node, ast.BinOp):
            result = _reconstruct_binop_concat(node)
            if result is not None:
                reconstructed_text, consumed = result
                if SQL_PATTERN.search(reconstructed_text):
                    snippet = reconstructed_text[:40]
                    if len(reconstructed_text) > 40:
                        snippet += "..."
                    violations.append(
                        f"{filename}:{node.lineno}: SQL string literal '{snippet}'"
                    )
                # Only the leaf Constants actually folded into the
                # reconstruction are consumed - never descendants of a
                # FormattedValue expression.
                consumed_constants |= consumed

        # L2: Check for SQL in plain string literals (skip if consumed by
        # compound reconstruction, or if it's a docstring)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in consumed_constants and id(node) not in docstring_constants:
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

        # L4: Check for private/protected attribute access
        elif isinstance(node, ast.Attribute):
            is_self_or_cls = isinstance(node.value, ast.Name) and node.value.id in (
                "self",
                "cls",
            )
            if node.attr.startswith("_") and not is_self_or_cls:
                violations.append(
                    f"{filename}:{node.lineno}: private attribute access '{node.attr}'"
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
