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
production), while over-inclusion is merely a spurious revalidation. Two
choices below lean deliberately toward over-inclusion:

- Search-root order (rule 4) tries ``repo_root`` first, then the importing
  file's own directory. The second root can resolve a name Python itself
  would resolve differently (or not at all) depending on ``sys.path`` at run
  time, but skipping it risks missing a real in-repo dependency.
- ``from pkg import name`` (rule 3) is expanded to include both ``pkg`` and
  ``pkg.name`` as candidate dotted names, because ``name`` may be a
  submodule (a file) rather than an attribute of ``pkg`` — the two are
  indistinguishable from the import statement's syntax alone. The same
  applies to relative imports: ``from . import name`` always includes the
  bare enclosing package as a candidate too, not only when the import also
  names a submodule (``from .mod import name``) — real Python executes the
  package's ``__init__.py`` either way, so treating the two forms
  asymmetrically would under-include exactly the file this module exists to
  stop missing.

Dynamic-import constructs (``importlib``, ``__import__``, ``exec``, ``eval``,
``.import_module``) are rejected outright rather than degrading to "resolve
what we can": whatever a script imports through one of those, this static
analysis cannot see, so the hash could never be trusted to reflect it.
"""

from __future__ import annotations

import ast
from collections import deque
from pathlib import Path

from continuo_python_runtime.errors import ContractError

_DYNAMIC_IMPORT_NAMES = frozenset({"__import__", "exec", "eval"})


def dynamic_import_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return `(lineno, construct)` for every dynamic-import construct in *tree*.

    `construct` is the offending name as written: "importlib", "__import__",
    "exec", "eval", or "import_module". Sorted by lineno, then construct.
    """
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == "importlib" for alias in node.names):
                violations.append((node.lineno, "importlib"))
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None and node.module.split(".")[0] == "importlib":
                violations.append((node.lineno, "importlib"))
        elif isinstance(node, ast.Name):
            if node.id in _DYNAMIC_IMPORT_NAMES:
                violations.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute):
            if node.attr == "import_module":
                violations.append((node.lineno, "import_module"))
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


def _resolve_name(name: str, repo_root: Path, importing_dir: Path) -> tuple[Path, Path, tuple[str, ...]] | None:
    """Resolve a dotted module name to (root, resolved file, name parts).

    Tries `repo_root` then `importing_dir` as search roots, and for each,
    `<root>/a/b/c.py` then `<root>/a/b/c/__init__.py`. Returns None when the
    name resolves under neither root (it's external).
    """
    parts = tuple(name.split("."))
    for root in (repo_root, importing_dir):
        module_file = root.joinpath(*parts).with_suffix(".py")
        if module_file.is_file():
            return root, module_file.resolve(), parts
        package_init = root.joinpath(*parts, "__init__.py")
        if package_init.is_file():
            return root, package_init.resolve(), parts
    return None


def resolve_closure(script_path: Path, repo_root: Path) -> list[Path]:
    """Return the transitive in-repo import closure of *script_path*.

    Absolute, resolved paths, sorted, with *script_path* itself EXCLUDED (it is
    `source_hash`). Files that do not resolve under *repo_root* — stdlib,
    site-packages, anything installed — are not closure members: external deps
    are the image's concern (`image_tag`), not the hash's.
    """
    repo_root = repo_root.resolve()
    script_resolved = script_path.resolve()

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

        importing_dir = current.parent
        for name in _collect_import_names(tree, current, repo_root):
            match = _resolve_name(name, repo_root, importing_dir)
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
