"""Tests for the static in-repo import-closure resolver."""

import pytest

from continuo_python_runtime.closure import dynamic_import_violations, resolve_closure
from continuo_python_runtime.errors import ContractError
import ast
import os


def test_no_imports_returns_empty(tmp_path):
    repo = tmp_path
    (repo / "node.py").write_text("def run():\n    return 1\n")

    assert resolve_closure(repo / "node.py", repo) == []


def test_simple_import_resolves(tmp_path):
    repo = tmp_path
    (repo / "node.py").write_text("import helpers\n")
    (repo / "helpers.py").write_text("X = 1\n")

    result = resolve_closure(repo / "node.py", repo)

    assert result == [(repo / "helpers.py").resolve()]


def test_package_import_includes_init(tmp_path):
    repo = tmp_path
    (repo / "scripts").mkdir()
    (repo / "scripts" / "node.py").write_text("import scripts.helpers\n")
    (repo / "scripts" / "helpers.py").write_text("X = 1\n")
    (repo / "scripts" / "__init__.py").write_text("")

    result = resolve_closure(repo / "scripts" / "node.py", repo)

    assert result == sorted(
        [
            (repo / "scripts" / "helpers.py").resolve(),
            (repo / "scripts" / "__init__.py").resolve(),
        ]
    )


def test_script_directory_is_a_search_root(tmp_path):
    """When the name doesn't resolve under repo_root, the importing file's
    own directory is tried as a second search root."""
    repo = tmp_path
    (repo / "scripts").mkdir()
    (repo / "scripts" / "node.py").write_text("import helpers\n")
    (repo / "scripts" / "helpers.py").write_text("X = 1\n")

    result = resolve_closure(repo / "scripts" / "node.py", repo)

    assert result == [(repo / "scripts" / "helpers.py").resolve()]


def test_package_preferred_over_same_named_module_file(tmp_path):
    """When both <root>/helpers.py and <root>/helpers/__init__.py exist,
    Python's import machinery selects the package - _resolve_name must match
    that lookup order (package before module file), or the package code that
    actually executes goes missing from the closure."""
    repo = tmp_path
    (repo / "node.py").write_text("import helpers\n")
    (repo / "helpers.py").write_text("X = 'module-file'\n")
    (repo / "helpers").mkdir()
    (repo / "helpers" / "__init__.py").write_text("X = 'package'\n")

    result = resolve_closure(repo / "node.py", repo)

    assert result == [(repo / "helpers" / "__init__.py").resolve()]


def test_search_roots_fixed_at_script_dir_not_importing_file_dir(tmp_path):
    """scripts/node.py does `from lib.shared import helper`; lib/shared.py
    does `import util`; only scripts/util.py exists (not lib/util.py). The
    harness puts (repo_root, script_dir) on sys.path for the whole process -
    not (repo_root, importing_file's own dir) - so `import util` inside
    lib/shared.py resolves against scripts/, the seed script's directory,
    not lib/. A resolver that used the current file's own parent as the
    second root would miss scripts/util.py entirely, hashing a different
    file than the one that actually runs."""
    repo = tmp_path
    (repo / "scripts").mkdir()
    (repo / "lib").mkdir()
    (repo / "scripts" / "node.py").write_text("from lib.shared import helper\n")
    (repo / "lib" / "shared.py").write_text("import util\n")
    (repo / "scripts" / "util.py").write_text("X = 1\n")

    result = resolve_closure(repo / "scripts" / "node.py", repo)

    assert (repo / "scripts" / "util.py").resolve() in result
    assert (repo / "lib" / "shared.py").resolve() in result


def test_script_at_repo_root_dedupes_search_roots(tmp_path):
    """When the script lives at repo_root, script_dir == repo_root, so the
    fixed (repo_root, script_dir) pair collapses to one root. Resolution
    must still work, and no candidate may be probed (or the result list
    duplicated) twice."""
    repo = tmp_path
    (repo / "node.py").write_text("import helpers\n")
    (repo / "helpers.py").write_text("X = 1\n")

    result = resolve_closure(repo / "node.py", repo)

    assert result == [(repo / "helpers.py").resolve()]
    assert len(result) == len(set(result))


def test_transitive_closure(tmp_path):
    repo = tmp_path
    (repo / "node.py").write_text("import a\n")
    (repo / "a.py").write_text("import b\n")
    (repo / "b.py").write_text("X = 1\n")

    result = resolve_closure(repo / "node.py", repo)

    assert result == sorted([(repo / "a.py").resolve(), (repo / "b.py").resolve()])


def test_cycle_terminates_and_dedupes(tmp_path):
    repo = tmp_path
    (repo / "node.py").write_text("import a\n")
    (repo / "a.py").write_text("import b\n")
    (repo / "b.py").write_text("import a\n")

    result = resolve_closure(repo / "node.py", repo)

    assert result == sorted([(repo / "a.py").resolve(), (repo / "b.py").resolve()])
    assert len(result) == 2


def test_script_itself_never_a_member_even_when_imported_back(tmp_path):
    repo = tmp_path
    (repo / "node.py").write_text("import a\n")
    (repo / "a.py").write_text("import node\n")

    result = resolve_closure(repo / "node.py", repo)

    assert result == [(repo / "a.py").resolve()]


def test_stdlib_and_external_packages_are_not_members(tmp_path):
    repo = tmp_path
    (repo / "node.py").write_text("import json\nimport pyarrow\n")

    assert resolve_closure(repo / "node.py", repo) == []


def test_absolute_from_import_expands_alias_to_submodule(tmp_path):
    """`from pkg import mod` may be importing a submodule (a file), not an
    attribute of pkg - the level-0 branch must try `pkg.mod` as a candidate
    alongside the bare `pkg` name, or the submodule file goes missing from
    the closure."""
    repo = tmp_path
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "mod.py").write_text("X = 1\n")
    (repo / "node.py").write_text("from pkg import mod\n")

    result = resolve_closure(repo / "node.py", repo)

    assert result == sorted(
        [
            (repo / "pkg" / "mod.py").resolve(),
            (repo / "pkg" / "__init__.py").resolve(),
        ]
    )


def test_relative_import_of_sibling_resolves(tmp_path):
    repo = tmp_path
    (repo / "scripts").mkdir()
    (repo / "scripts" / "__init__.py").write_text("")
    (repo / "scripts" / "node.py").write_text("from . import sibling\n")
    (repo / "scripts" / "sibling.py").write_text("X = 1\n")

    result = resolve_closure(repo / "scripts" / "node.py", repo)

    assert result == sorted(
        [
            (repo / "scripts" / "sibling.py").resolve(),
            (repo / "scripts" / "__init__.py").resolve(),
        ]
    )


def test_relative_import_of_package_attribute_includes_init(tmp_path):
    """`from . import NAME` executes the package's __init__.py regardless of
    whether NAME is a submodule file or merely an attribute defined inside
    __init__.py - the bare package prefix must always be a resolution
    candidate, not only when a `node.module` part is also present."""
    repo = tmp_path
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("CONSTANT = 1\n")
    (repo / "pkg" / "node.py").write_text("from . import CONSTANT\n")

    result = resolve_closure(repo / "pkg" / "node.py", repo)

    assert result == [(repo / "pkg" / "__init__.py").resolve()]


def test_relative_import_escaping_repo_root_is_skipped_not_an_error(tmp_path):
    """`from .. import other` from a top-level script would need to walk
    above repo_root. It must be silently skipped, not raise.

    `other.py` is planted at repo_root's own top level - exactly where a
    buggy implementation that clamped the walk-up instead of detecting the
    escape would wrongly resolve it (Python slicing clamps out-of-range
    negative lengths instead of erroring, so this is a real risk, not a
    hypothetical one)."""
    repo_root = tmp_path
    (repo_root / "other.py").write_text("X = 1\n")
    (repo_root / "node.py").write_text("from .. import other\n")

    result = resolve_closure(repo_root / "node.py", repo_root)

    assert result == []


def test_symlinked_file_escaping_repo_root_is_excluded(tmp_path):
    """Rule 6's `is_relative_to` check, applied after `resolve()`, is the
    only thing stopping a symlink from smuggling an out-of-repo file into
    the closure (and therefore into the hash). A regression that deleted
    that check would leave every other test in this file green."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "helpers.py").write_text("X = 1\n")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "node.py").write_text("import helpers\n")
    os.symlink(os.path.join("..", "outside", "helpers.py"), repo / "helpers.py")

    assert resolve_closure(repo / "node.py", repo) == []


def test_symlinked_package_directory_escaping_repo_root_is_excluded(tmp_path):
    """Same guard, pinned through the ancestor-__init__.py resolution path
    (rule 5) rather than the primary candidate - a symlinked package
    directory must not smuggle its __init__.py into the closure either."""
    outside = tmp_path / "outside_pkg"
    outside.mkdir()
    (outside / "__init__.py").write_text("")
    (outside / "helpers.py").write_text("X = 1\n")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "node.py").write_text("import pkg.helpers\n")
    os.symlink(os.path.join("..", "outside_pkg"), repo / "pkg", target_is_directory=True)

    assert resolve_closure(repo / "node.py", repo) == []


DYNAMIC_IMPORT_SOURCES = [
    pytest.param("import importlib\n", id="import-importlib"),
    pytest.param("from importlib import import_module\n", id="from-importlib-import-import_module"),
    pytest.param('__import__("x")\n', id="dunder-import-call"),
    pytest.param('exec("x")\n', id="exec-call"),
    pytest.param('eval("x")\n', id="eval-call"),
    pytest.param('import importlib as il\nil.import_module("x")\n', id="aliased-import_module-attr"),
]


@pytest.mark.parametrize("source", DYNAMIC_IMPORT_SOURCES)
def test_dynamic_import_construct_rejected_in_script(tmp_path, source):
    repo = tmp_path
    (repo / "node.py").write_text(source)

    with pytest.raises(ContractError):
        resolve_closure(repo / "node.py", repo)


def test_aliased_builtins_import_dunder_import_rejected(tmp_path):
    """`from builtins import __import__ as load` then `load("helpers")`
    creates no `ast.Name` named `__import__` (the alias is `load`), and the
    old ImportFrom branch checked only `importlib` - this passed lint and
    merge while the dynamically loaded helper was omitted from the closure."""
    repo = tmp_path
    (repo / "node.py").write_text("from builtins import __import__ as load\nload('x')\n")

    with pytest.raises(ContractError):
        resolve_closure(repo / "node.py", repo)


def test_import_builtins_rejected(tmp_path):
    repo = tmp_path
    (repo / "node.py").write_text("import builtins\n")

    with pytest.raises(ContractError):
        resolve_closure(repo / "node.py", repo)


def test_builtins_dunder_import_attribute_call_rejected(tmp_path):
    repo = tmp_path
    (repo / "node.py").write_text('import builtins\nbuiltins.__import__("x")\n')

    with pytest.raises(ContractError):
        resolve_closure(repo / "node.py", repo)


def test_aliased_exec_reexport_from_arbitrary_module_rejected(tmp_path):
    """`from somewhere import exec as e` re-exports a dynamic-import-capable
    name from a module that is neither `importlib` nor `builtins` - the
    alias.name check must catch this regardless of the source module."""
    repo = tmp_path
    (repo / "somewhere").mkdir()
    (repo / "somewhere" / "__init__.py").write_text("def exec(*args, **kwargs):\n    return None\n")
    (repo / "node.py").write_text("from somewhere import exec as e\n")

    with pytest.raises(ContractError):
        resolve_closure(repo / "node.py", repo)


def test_dataframe_eval_and_query_methods_not_flagged(tmp_path):
    """pandas DataFrame.eval / DataFrame.query are legitimate in a node
    script that returns a dataframe - flagging df.eval(...) would reject
    ordinary user code. Only builtins.exec/eval (caught by the module rule)
    and bare Name usage are dynamic-import constructs; an arbitrary object's
    .eval/.query attribute is not."""
    repo = tmp_path
    (repo / "node.py").write_text(
        "def run(df):\n    a = df.eval('a + b')\n    b = df.query('a > 1')\n    return a, b\n"
    )

    assert resolve_closure(repo / "node.py", repo) == []
    assert dynamic_import_violations(ast.parse(repo.joinpath("node.py").read_text())) == []


def test_dynamic_import_construct_rejected_in_closure_member(tmp_path):
    """The construct doesn't have to be in the script itself - a file the
    script imports is scanned too."""
    repo = tmp_path
    (repo / "node.py").write_text("import helpers\n")
    (repo / "helpers.py").write_text('exec("x")\n')

    with pytest.raises(ContractError):
        resolve_closure(repo / "node.py", repo)


def test_syntax_error_in_closure_member_raises(tmp_path):
    repo = tmp_path
    (repo / "node.py").write_text("import helpers\n")
    (repo / "helpers.py").write_text("def broken(:\n")

    with pytest.raises(ContractError):
        resolve_closure(repo / "node.py", repo)


def test_syntax_error_message_has_no_lineno_and_uses_repo_relative_path(tmp_path):
    repo = tmp_path
    (repo / "scripts").mkdir()
    (repo / "scripts" / "node.py").write_text("def broken(:\n")

    with pytest.raises(ContractError, match=r"^scripts/node\.py: syntax error: "):
        resolve_closure(repo / "scripts" / "node.py", repo)


def test_dynamic_import_error_message_format(tmp_path):
    repo = tmp_path
    (repo / "node.py").write_text('exec("x")\n')

    with pytest.raises(
        ContractError,
        match=r"^node\.py:1: dynamic import construct 'exec' is not allowed",
    ):
        resolve_closure(repo / "node.py", repo)


def test_determinism_sorted_and_stable(tmp_path):
    repo = tmp_path
    (repo / "node.py").write_text("import a\nimport b\nimport c\n")
    (repo / "a.py").write_text("X = 1\n")
    (repo / "b.py").write_text("X = 1\n")
    (repo / "c.py").write_text("X = 1\n")

    first = resolve_closure(repo / "node.py", repo)
    second = resolve_closure(repo / "node.py", repo)

    assert first == second
    assert first == sorted(first)


# --- dynamic_import_violations, tested directly (Task 5's linter consumes it) ---


def test_violations_empty_for_clean_tree():
    assert dynamic_import_violations(ast.parse("x = 1\n")) == []


def test_violations_reports_construct_names():
    tree = ast.parse(
        "import importlib\n"
        "il = importlib\n"
        "__import__('x')\n"
        "exec('x')\n"
        "eval('x')\n"
        "il.import_module('x')\n"
    )
    violations = dynamic_import_violations(tree)
    constructs = {construct for _lineno, construct in violations}
    assert constructs == {"importlib", "__import__", "exec", "eval", "import_module"}


def test_violations_sorted_by_lineno_then_construct():
    tree = ast.parse("exec('x'); eval('x')\n")
    assert dynamic_import_violations(tree) == [(1, "eval"), (1, "exec")]


def test_importlib_submodule_import_flagged():
    tree = ast.parse("import importlib.util\n")
    assert dynamic_import_violations(tree) == [(1, "importlib")]
