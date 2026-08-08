"""Static in-repo import-closure resolver.

CI's ``content_hash`` is the sole change detector Continuo uses to decide
whether a node needs revalidation. Until this module existed, that hash
covered only the node's own script file, so a byte edit to a shared helper
module the script imports changed nothing — the node kept running against a
stale fingerprint in production. ``resolve_closure`` closes that gap: it
returns the transitive set of in-repo Python files a script reaches through
its ``import`` statements, so a later stage can fold their bytes into the
hash too.

That framing decides every ambiguous call in the algorithm below:
under-inclusion is a correctness bug (a stale node silently running in
production), while over-inclusion is merely a spurious revalidation.

- Search roots (rule 4) are the fixed, ordered pair ``(repo_root,
  script_dir)`` — ``script_dir`` computed once from the seed script and
  reused, unchanged, for every resolution in the traversal, never the
  current file's own directory. This mirrors
  :func:`continuo_python_runtime.harness.ensure_import_paths` exactly, which
  puts that same pair on ``sys.path`` for the process lifetime: the static
  model and the runtime model are provably the same list. A module importing
  a sibling that is on neither root (e.g. ``lib/shared.py`` doing ``import
  sibling`` for a file at ``lib/sibling.py``, when the script itself lives
  elsewhere) raises ``ImportError`` at run time, so excluding that sibling
  from the closure cannot hide a stale-node bug — that node cannot run at
  all. When the script lives at ``repo_root`` itself, the pair collapses to
  a single root so the same candidate is not probed twice.
- Within a search root, the package form (``<root>/a/b/c/__init__.py``) is
  tried before the module-file form (``<root>/a/b/c.py``), matching
  Python's own lookup order: when both exist, ``import a.b.c`` binds the
  package, and the module file of the same name is never executed.
- ``from pkg import name`` (rule 3) is expanded to include both ``pkg`` and
  ``pkg.name`` as candidate dotted names, because ``name`` may be a
  submodule (a file) rather than an attribute of ``pkg`` — the two are
  indistinguishable from the import statement's syntax alone. The same
  applies to relative imports: ``from . import name`` always includes the
  bare enclosing package as a candidate too, not only when the import also
  names a submodule (``from .mod import name``) — real Python executes the
  package's ``__init__.py`` either way, so treating the two forms
  asymmetrically would under-include exactly the file this module exists to
  stop missing. This choice leans deliberately toward over-inclusion:
  ``name`` may turn out to be a plain attribute rather than a submodule, in
  which case the extra candidate simply fails to resolve.

Dynamic-import constructs (``importlib``, ``builtins``, ``__import__``,
``exec``, ``eval``, ``.import_module``) are rejected outright rather than
degrading to "resolve what we can": whatever a script imports through one of
those, this static analysis cannot see, so the hash could never be trusted
to reflect it.
"""

from __future__ import annotations

import ast
from collections import deque
from pathlib import Path

from continuo_python_runtime.errors import ContractError

_DYNAMIC_IMPORT_MODULES = frozenset({"importlib", "builtins"})
_DYNAMIC_IMPORT_NAMES = frozenset({"__import__", "exec", "eval"})
_DYNAMIC_IMPORT_ATTRS = frozenset({"import_module", "__import__"})


def dynamic_import_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return `(lineno, construct)` for every dynamic-import construct in *tree*.

    Flags, per AST node type:

    - `ast.Import`: any alias whose root module (`name.split(".")[0]`) is
      `importlib` or `builtins` — e.g. `import importlib.util`, `import
      builtins`. `construct` is that root module name.
    - `ast.ImportFrom`: if the root module is `importlib` or `builtins`,
      one violation naming that module — and nothing else from the same
      statement (one statement, one violation; its aliases are not also
      inspected). Otherwise, any `alias.name` in `{"__import__", "exec",
      "eval"}` is a violation named for that alias — this catches
      re-exports of those names from a module that is neither `importlib`
      nor `builtins` (e.g. `from somewhere import exec as e`), which no
      other rule here would see.
    - `ast.Name`: `id` in `{"__import__", "exec", "eval"}` (a bare
      reference or call).
    - `ast.Attribute`: `attr` in `{"import_module", "__import__"}`.
      Deliberately NOT `exec`/`eval`: those are legitimate method names on
      arbitrary objects (pandas `DataFrame.eval`/`DataFrame.query`, used in
      ordinary node scripts that return a dataframe), and flagging them here
      would reject that legitimate user code. `builtins.exec` /
      `builtins.eval` are still caught — not by this rule, but because the
      `import builtins` that must precede them is itself an `ast.Import`
      violation above. Do not "fix" this by adding them here.

    `construct` is the offending name as written: "importlib", "builtins",
    "__import__", "exec", "eval", or "import_module". Sorted by lineno, then
    construct.
    """
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _DYNAMIC_IMPORT_MODULES:
                    violations.append((node.lineno, root))
        elif isinstance(node, ast.ImportFrom):
            from_root = node.module.split(".")[0] if node.module is not None else None
            if from_root in _DYNAMIC_IMPORT_MODULES:
                violations.append((node.lineno, from_root))
                continue  # one statement, one violation - aliases not also checked
            for alias in node.names:
                if alias.name in _DYNAMIC_IMPORT_NAMES:
                    violations.append((node.lineno, alias.name))
        elif isinstance(node, ast.Name):
            if node.id in _DYNAMIC_IMPORT_NAMES:
                violations.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute):
            if node.attr in _DYNAMIC_IMPORT_ATTRS:
                violations.append((node.lineno, node.attr))
    return sorted(violations)


def _package_components(importing_file: Path, repo_root: Path) -> tuple[str, ...]:
    """Dotted-path components of *importing_file*'s parent directory,
    relative to *repo_root* (empty tuple when the file lives at the root)."""
    rel_parent = importing_file.parent.relative_to(repo_root)
    if rel_parent == Path("."):
        return ()
    return rel_parent.parts


def _names_from_import(node: ast.Import) -> list[str]:
    return [alias.name for alias in node.names]


def _names_from_import_from(
    node: ast.ImportFrom, importing_file: Path, repo_root: Path
) -> list[str]:
    if node.level == 0:
        # level == 0 always carries a module per the grammar (`from X import Y`).
        base = node.module
        assert base is not None
        return [base] + [f"{base}.{alias.name}" for alias in node.names]

    components = _package_components(importing_file, repo_root)
    walk_up = node.level - 1
    if walk_up > len(components):
        return []  # would escape repo_root - cannot name an in-repo file

    prefix_parts = components[: len(components) - walk_up]
    prefix = ".".join(prefix_parts)
    base = ".".join(part for part in (prefix, node.module) if part)

    # Real Python executes the package's __init__.py for `from . import name`
    # regardless of whether `name` is a submodule file or just an attribute
    # defined inside __init__.py - the two are indistinguishable from the
    # import statement's syntax alone, so the bare prefix/base is always a
    # candidate (symmetric with the level == 0 branch above), not only when
    # node.module is also present. Missing __init__.py here would be
    # under-inclusion (a stale node in production); including it when it was
    # already reachable is a no-op, and including it spuriously costs at
    # most one revalidation - see the module docstring.
    names = [base] if base else []
    names.extend(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    return names


def _collect_import_names(tree: ast.AST, importing_file: Path, repo_root: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(_names_from_import(node))
        elif isinstance(node, ast.ImportFrom):
            names.extend(_names_from_import_from(node, importing_file, repo_root))
    return names


def _resolve_name(
    name: str, search_roots: tuple[Path, ...]
) -> tuple[Path, Path, tuple[str, ...]] | None:
    """Resolve a dotted module name to (root, resolved file, name parts).

    Tries each root in *search_roots*, in order, and for each, the package
    form `<root>/a/b/c/__init__.py` before the module-file form
    `<root>/a/b/c.py` - matching Python's own package-before-module lookup
    order (when both exist, `import a.b.c` binds the package and the module
    file of the same name is never executed). Returns None when the name
    resolves under no root (it's external).
    """
    parts = tuple(name.split("."))
    for root in search_roots:
        package_init = root.joinpath(*parts, "__init__.py")
        if package_init.is_file():
            return root, package_init.resolve(), parts
        module_file = root.joinpath(*parts).with_suffix(".py")
        if module_file.is_file():
            return root, module_file.resolve(), parts
    return None


def resolve_closure(script_path: Path, repo_root: Path) -> list[Path]:
    """Return the transitive in-repo import closure of *script_path*.

    Absolute, resolved paths, sorted, with *script_path* itself EXCLUDED (it is
    `source_hash`). Files that do not resolve under *repo_root* — stdlib,
    site-packages, anything installed — are not closure members: external deps
    are the image's concern (`image_tag`), not the hash's.

    Search roots are the fixed, ordered pair `(repo_root, script_dir)` -
    `script_dir` is *script_path*'s own directory, computed once here and
    reused for every resolution in the traversal, matching what
    `harness.ensure_import_paths` puts on `sys.path` for the whole process.
    When the script lives at *repo_root* itself the pair collapses to a
    single root, so it is not probed twice.
    """
    repo_root = repo_root.resolve()
    script_resolved = script_path.resolve()
    script_dir = script_resolved.parent
    search_roots = (repo_root,) if script_dir == repo_root else (repo_root, script_dir)

    seen: set[Path] = {script_resolved}
    queue: deque[Path] = deque([script_resolved])

    while queue:
        current = queue.popleft()
        rel = current.relative_to(repo_root)

        source = current.read_bytes()
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            raise ContractError(f"{rel}: syntax error: {exc.msg}") from exc

        violations = dynamic_import_violations(tree)
        if violations:
            lineno, construct = violations[0]
            raise ContractError(
                f"{rel}:{lineno}: dynamic import construct {construct!r} is not "
                "allowed — the content hash cannot see it"
            )

        for name in _collect_import_names(tree, current, repo_root):
            match = _resolve_name(name, search_roots)
            if match is None:
                continue
            root, resolved_file, parts = match

            candidates = [resolved_file]
            for depth in range(1, len(parts)):
                init_candidate = root.joinpath(*parts[:depth], "__init__.py")
                if init_candidate.is_file():
                    candidates.append(init_candidate.resolve())

            for candidate in candidates:
                if not candidate.is_relative_to(repo_root):
                    continue  # symlink escape
                if candidate in seen:
                    continue
                seen.add(candidate)
                queue.append(candidate)

    return sorted(seen - {script_resolved})
