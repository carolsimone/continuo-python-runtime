"""Guard: every test in this repo is selected by a CI workflow.

A test that no workflow runs is a test that cannot fail. The risk is not today's
tests but tomorrow's: a new test directory, a new marker, or a new file with a
marker that an explicit file list in a workflow does not name. This guard fails
when that happens, so CI coverage cannot silently shrink.

It reads the workflow files as text on purpose: the assertions are about the
commands CI runs, and a YAML round trip would add nothing to them.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = (ROOT / ".github/workflows/ci.yml").read_text()
IMAGES = (ROOT / ".github/workflows/images.yml").read_text()

# Directories that never hold this repo's own tests.
_SKIP_DIRS = {".git", ".venv", ".claude", ".worktrees", ".superpowers", "node_modules",
              "wheelhouse", "dist", "build", "__pycache__", ".mypy_cache", ".ruff_cache",
              ".pytest_cache"}
# pytest's own markers: they never route a test to a different CI step.
_BUILTIN_MARKS = {"parametrize", "skip", "skipif", "xfail", "usefixtures", "filterwarnings"}
# The custom markers pyproject declares, each of which a workflow must select.
_ROUTED_MARKS = {"integration", "image"}


def _test_files() -> list[Path]:
    found = []
    for pattern in ("test_*.py", "*_test.py"):
        for path in ROOT.rglob(pattern):
            if not any(part in _SKIP_DIRS for part in path.relative_to(ROOT).parts):
                found.append(path)
    return sorted(set(found))


def _test_roots() -> list[str]:
    """Each test root: the top-level tests/, contract/tests and adapters/<engine>/tests."""
    roots = ["tests", "contract/tests"]
    roots += sorted(f"adapters/{p.name}/tests" for p in (ROOT / "adapters").iterdir() if (p / "tests").is_dir())
    return roots


def test_every_test_file_lives_under_a_known_test_root():
    roots = tuple(f"{root}/" for root in _test_roots())
    stray = [str(p.relative_to(ROOT)) for p in _test_files()
             if not str(p.relative_to(ROOT)).startswith(roots)]
    assert not stray, (
        f"test files outside every test root CI runs: {stray}; move them under one of "
        f"{_test_roots()} or add their directory to .github/workflows/ci.yml and this guard"
    )


def _ci_pytest_commands() -> list[str]:
    return [line.strip() for line in CI.splitlines() if "uv run pytest" in line]


def _runs(root: str, *, marker_expr: str | None) -> bool:
    """Whether some ci.yml pytest command names *root* and (optionally) selects *marker_expr*."""
    for command in _ci_pytest_commands():
        if root in command.split() and (marker_expr is None or f"-m {marker_expr}" in command):
            return True
    return False


def test_every_test_root_is_run_by_ci():
    # The top-level tests/ is run bare (`pytest ...` uses testpaths = ["tests"]).
    for root in _test_roots():
        if root != "tests":
            assert _runs(root, marker_expr=None), (
                f"{root} is not named in any pytest command of .github/workflows/ci.yml"
            )


def test_root_unit_tests_are_run_by_ci():
    assert 'pytest --cov=continuo_python_runtime -m "not image and not integration"' in CI


def test_root_integration_tests_are_selected_by_marker_not_by_file_name():
    """An explicit file list silently drops every new integration test at the repo root."""
    assert "pytest tests -m integration" in CI, (
        "the root integration step must select by marker (`pytest tests -m integration`), "
        "not by a hard-coded list of files"
    )
    assert "tests/test_csv_readers_integration.py" not in CI


def test_every_adapter_runs_its_integration_tests_in_ci():
    for root in _test_roots():
        if not root.startswith("adapters/"):
            continue
        marked = any(
            re.search(r"pytest\.mark\.integration", p.read_text())
            for p in (ROOT / root).glob("test_*.py")
        )
        if marked:
            assert _runs(root, marker_expr="integration"), (
                f"{root} has integration-marked tests but no `pytest {root} -m integration` step in ci.yml"
            )


def test_every_adapter_runs_its_unit_tests_in_ci():
    for root in _test_roots():
        if root.startswith("adapters/"):
            assert _runs(root, marker_expr='"not integration"'), (
                f'{root} has no `pytest {root} -m "not integration"` step in ci.yml'
            )


def test_image_tests_run_in_the_image_workflow_for_every_engine():
    engines = sorted(p.name for p in (ROOT / "adapters").iterdir() if (p / "tests").is_dir())
    for engine in engines:
        assert f"VALIDATION_IMAGE_ENGINE: {engine}" in IMAGES, (
            f"images.yml has no image smoke job for engine {engine!r}"
        )
    assert IMAGES.count("-m image") >= len(engines)


def test_the_image_workflow_runs_when_the_image_tests_or_pins_change():
    for path in ("tests/test_image_smoke_validation.py", "image-requirements-*.txt", "scripts/**"):
        assert f'"{path}"' in IMAGES, f"images.yml pull_request paths do not include {path}"


def test_every_custom_marker_in_use_is_routed_to_a_workflow():
    """A new marker would deselect its tests from the `not integration`/`not image` steps
    without any workflow selecting them: fail until CI knows about it."""
    used = set()
    for path in _test_files():
        used |= set(re.findall(r"pytest\.mark\.(\w+)", path.read_text()))
    custom = used - _BUILTIN_MARKS
    assert custom <= _ROUTED_MARKS, (
        f"markers {sorted(custom - _ROUTED_MARKS)} are used by tests but no CI step selects them; "
        f"route them in .github/workflows and add them to _ROUTED_MARKS here"
    )
