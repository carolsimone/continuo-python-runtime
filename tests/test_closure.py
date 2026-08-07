"""Tests for the static in-repo import-closure resolver."""

import pytest

from continuo_python_runtime.closure import dynamic_import_violations, resolve_closure
from continuo_python_runtime.errors import ContractError
import ast


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
    tree = ast.parse("exec('x')\neval('x')\n")
    assert dynamic_import_violations(tree) == [(1, "exec"), (2, "eval")]


def test_importlib_submodule_import_flagged():
    tree = ast.parse("import importlib.util\n")
    assert dynamic_import_violations(tree) == [(1, "importlib")]
