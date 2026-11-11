"""Script linting for forbidden imports, SQL literals, data-access calls,
dynamic-import constructs, and code the content hash cannot see."""

import ast
import re
from pathlib import Path

from continuo_python_runtime.closure import dynamic_import_violations

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

# Modules whose import runs code the import closure never reaches.
_STARTUP_HOOK_MODULES = {"sitecustomize", "usercustomize"}
_STARTUP_HOOK_FILES = {f"{module}.py" for module in _STARTUP_HOOK_MODULES}

# ``list`` methods that change ``sys.path`` in place.
_PATH_MUTATORS = {"append", "insert", "extend", "remove", "pop", "clear", "reverse", "sort"}

# Names ``pickle`` exports that rebuild objects, and so run code, from bytes.
_PICKLE_LOADERS = {"load", "loads", "Unpickler"}


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


def _module_aliases(tree: ast.AST, module: str) -> set[str]:
    """Local names bound to *module* by ``import module`` / ``import module as x``."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == module
    }


def _from_imports(tree: ast.AST, module: str) -> dict[str, str]:
    """``{local name: imported name}`` for every ``from module import name [as x]``."""
    return {
        alias.asname or alias.name: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == module
        for alias in node.names
    }


def _assignment_targets(node: ast.AST) -> list[ast.expr]:
    """Every expression *node* (an assignment or ``del``) writes to, with
    tuple and list targets unpacked."""
    if isinstance(node, ast.Assign):
        pending = list(node.targets)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        pending = [node.target]
    elif isinstance(node, ast.Delete):
        pending = list(node.targets)
    else:
        return []
    targets: list[ast.expr] = []
    while pending:
        target = pending.pop()
        if isinstance(target, (ast.Tuple, ast.List)):
            pending.extend(target.elts)
        elif isinstance(target, ast.Starred):
            pending.append(target.value)
        else:
            targets.append(target)
    return targets


def unhashed_code_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, construct)`` for every construct in *tree* that runs
    code the content hash does not cover.

    The hash folds the script and the files it imports, found by reading
    imports. A node that executes anything else, or changes where imports
    resolve, can change its behaviour without changing its hash. Flagged
    constructs, by name:

    - ``runpy``: any import of it, which runs a file by path.
    - ``sys.path``: a call that mutates it (``append``, ``insert``, ``extend``,
      ``remove``, ``pop``, ``clear``, ``reverse``, ``sort``), an assignment,
      augmented assignment or ``del`` of it or of a subscript of it, and
      ``site.addsitedir``. ``import sys as s`` and ``from sys import path``
      are followed.
    - ``sitecustomize`` and ``usercustomize``: any import of either.
    - ``compile``: a call of the builtin, by its bare name (a ``compile``
      imported from a module, such as ``re.compile``, is not the builtin).
    - ``FunctionType``: ``types.FunctionType`` and the name imported from
      ``types``.
    - ``pickle.load``, ``pickle.loads`` and ``pickle.Unpickler``: attributes of
      ``pickle`` or of an alias of it, and the names imported from it.

    Names are resolved by the file's imports alone, not by scope or by
    data flow: ``p = sys.path`` followed by ``p.append(...)`` is not seen.

    Sorted by lineno, then construct.
    """
    sys_names = _module_aliases(tree, "sys")
    site_names = _module_aliases(tree, "site")
    types_names = _module_aliases(tree, "types")
    pickle_names = _module_aliases(tree, "pickle")
    sys_path_names = {local for local, name in _from_imports(tree, "sys").items() if name == "path"}
    addsitedir_names = {
        local for local, name in _from_imports(tree, "site").items() if name == "addsitedir"
    }
    # ``from re import compile`` shadows the builtin.
    imported_names = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    def module_attribute(node: ast.expr, names: set[str], attr: str) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == attr
            and isinstance(node.value, ast.Name)
            and node.value.id in names
        )

    def is_sys_path(node: ast.expr) -> bool:
        return module_attribute(node, sys_names, "path") or (
            isinstance(node, ast.Name) and node.id in sys_path_names
        )

    def writes_sys_path(target: ast.expr, *, rebinding: bool) -> bool:
        """Whether writing to *target* changes ``sys.path``. Rebinding a bare
        name imported from ``sys`` only changes the local name."""
        if isinstance(target, ast.Subscript):
            return is_sys_path(target.value)
        if rebinding:
            return module_attribute(target, sys_names, "path")
        return is_sys_path(target)

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root == "runpy" or root in _STARTUP_HOOK_MODULES:
                    violations.append((node.lineno, root))
        elif isinstance(node, ast.ImportFrom):
            from_root = node.module.split(".")[0] if node.module and node.level == 0 else None
            if from_root == "runpy" or from_root in _STARTUP_HOOK_MODULES:
                violations.append((node.lineno, from_root))
            elif from_root == "types" and any(a.name == "FunctionType" for a in node.names):
                violations.append((node.lineno, "FunctionType"))
            elif from_root == "pickle":
                violations.extend(
                    (node.lineno, f"pickle.{a.name}")
                    for a in node.names
                    if a.name in _PICKLE_LOADERS
                )
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _PATH_MUTATORS and is_sys_path(
                func.value
            ):
                violations.append((node.lineno, "sys.path"))
            elif module_attribute(func, site_names, "addsitedir") or (
                isinstance(func, ast.Name) and func.id in addsitedir_names
            ):
                violations.append((node.lineno, "sys.path"))
            elif (
                isinstance(func, ast.Name)
                and func.id == "compile"
                and "compile" not in imported_names
            ):
                violations.append((node.lineno, "compile"))
        elif isinstance(node, ast.Attribute):
            if module_attribute(node, types_names, "FunctionType"):
                violations.append((node.lineno, "FunctionType"))
            elif node.attr in _PICKLE_LOADERS and module_attribute(node, pickle_names, node.attr):
                violations.append((node.lineno, f"pickle.{node.attr}"))
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
            rebinding = not isinstance(node, ast.AugAssign)
            if any(
                writes_sys_path(target, rebinding=rebinding)
                for target in _assignment_targets(node)
            ):
                violations.append((node.lineno, "sys.path"))
    return sorted(violations)


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
    - L5: dynamic-import construct (``importlib`` in any form, ``__import__``,
      ``exec``, ``eval``, or ``.import_module``). The content hash's import
      closure is computed by static AST analysis (see ``closure.py``); a
      construct that analysis cannot see must not exist, or a node could
      import a module whose edits never re-fingerprint it. Detection is
      delegated to ``continuo_python_runtime.closure.dynamic_import_violations``
      so the predicate is defined in exactly one place.
    - L6: unhashed-code construct (``runpy``, a change to ``sys.path``, a
      ``sitecustomize``/``usercustomize`` module, ``compile()``,
      ``types.FunctionType``, ``pickle.load``/``loads``/``Unpickler``), and a
      file named ``sitecustomize.py`` or ``usercustomize.py``. The import
      closure follows imports only, so code these constructs run is not in
      ``content_hash`` (C12). Detection is
      ``unhashed_code_violations``. A file a script reads belongs in the
      node's ``assets``, where it is hashed.

    Docstrings (the first statement of a Module/ClassDef/FunctionDef/
    AsyncFunctionDef body, when it is a bare string Expr) are exempt from the
    L2 constant pass, since prose commonly contains words like "select" and
    "from". This is a best-effort, position-based exemption: the same prose
    assigned to a variable is still flagged.

    Violations are returned sorted by line number; ties keep discovery order.
    """
    entries: list[tuple[int, str]] = []

    # L6: a startup hook runs before the harness, whatever it contains.
    if Path(filename).name in _STARTUP_HOOK_FILES:
        entries.append((1, f"unhashed code construct '{Path(filename).stem}'"))

    # Try to parse the source code
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        entries.append((e.lineno or 1, f"syntax error: {e.msg}"))
        return [f"{filename}:{lineno}: {message}" for lineno, message in entries]

    # Track imported data-access functions and their local aliases
    imported_forbidden_calls: dict[str, int] = {}  # {local_name: lineno}

    # First pass: collect information about imports
    for node in ast.walk(tree):
        # L1: Check for forbidden imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_module = alias.name.split(".")[0]
                if root_module in FORBIDDEN_DRIVERS:
                    entries.append(
                        (node.lineno, f"forbidden warehouse driver import '{root_module}'")
                    )

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root_module = node.module.split(".")[0]
                if root_module in FORBIDDEN_DRIVERS:
                    entries.append(
                        (node.lineno, f"forbidden warehouse driver import '{root_module}'")
                    )

            # Track imports of forbidden data-access functions
            if node.names:
                for alias in node.names:
                    # alias.name is the original name, alias.asname is the local alias (or None)
                    if alias.name in FORBIDDEN_CALLS:
                        local_name = alias.asname if alias.asname else alias.name
                        imported_forbidden_calls[local_name] = node.lineno
                        entries.append(
                            (node.lineno, f"forbidden data-access import '{alias.name}'")
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
                entries.append((node.lineno, f"SQL string literal '{snippet}'"))
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
                    entries.append((node.lineno, f"SQL string literal '{snippet}'"))
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
                    entries.append((node.lineno, f"SQL string literal '{snippet}'"))

        # L3: Check for forbidden data-access calls
        elif isinstance(node, ast.Call):
            # Attribute access: pd.read_sql(...)
            if isinstance(node.func, ast.Attribute):
                if node.func.attr in FORBIDDEN_CALLS:
                    entries.append(
                        (node.lineno, f"forbidden data-access call '{node.func.attr}'")
                    )

            # Name reference: read_sql_table(...) or rs(...) where rs is an alias
            elif isinstance(node.func, ast.Name):
                if node.func.id in imported_forbidden_calls:
                    entries.append(
                        (node.lineno, f"forbidden data-access call '{node.func.id}'")
                    )

        # L4: Check for private/protected attribute access
        elif isinstance(node, ast.Attribute):
            is_self_or_cls = isinstance(node.value, ast.Name) and node.value.id in (
                "self",
                "cls",
            )
            if node.attr.startswith("_") and not is_self_or_cls:
                entries.append((node.lineno, f"private attribute access '{node.attr}'"))

    # L5: dynamic-import constructs, reported via the shared closure.py
    # predicate (which raises on only the first hit; here every hit counts).
    for lineno, construct in dynamic_import_violations(tree):
        entries.append((lineno, f"dynamic import construct '{construct}'"))

    # L6: constructs that run code the hash cannot see.
    for lineno, construct in unhashed_code_violations(tree):
        entries.append((lineno, f"unhashed code construct '{construct}'"))

    # Stable sort by line number: this is what puts L5 and L6 hits "alongside"
    # the L1-L4 hits found above instead of trailing as a separate block, while
    # violations on the same line keep the order the passes above found them
    # (L1 first pass, then L2-L4 second pass, then L5, then L6).
    entries.sort(key=lambda entry: entry[0])
    return [f"{filename}:{lineno}: {message}" for lineno, message in entries]


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
        try:
            source = filepath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            violations.append(f"{filepath}: unreadable: {exc}")
            continue
        file_violations = lint_source(source, str(filepath))
        violations.extend(file_violations)

    return violations
