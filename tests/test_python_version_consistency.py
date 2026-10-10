"""`.python-version` is the single source of truth for the Python version.

Every other file that hardcodes the version must agree with it, so a bump is a
one-line edit to `.python-version` and CI catches any drift. These checks scan
by directory (``rglob``/``glob``), never by a hardcoded file list: a new
``pyproject.toml``, ``Dockerfile*`` or workflow is covered automatically. A new
*kind* of file that hardcodes the version (a shape these patterns do not match)
is the one thing that still needs a pattern added here.

Two tiers, because the version means different things in different files:
- full patch (``.python-version`` and the engine ``Dockerfile``s ``FROM
  python:X-slim``) — the exact interpreter dev, CI and the shipped image run;
- major.minor (``requires-python`` floor, mypy ``python_version``, trove
  classifiers, ``setup-python`` inputs) — a policy/compatibility statement, and
  mypy/``requires-python`` reject a patch component anyway.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = (ROOT / ".python-version").read_text().strip()  # e.g. "3.14.2"
MAJOR_MINOR = ".".join(CANONICAL.split(".")[:2])  # "3.14"

# Directories whose files are not ours to keep in sync (installed deps, VCS,
# build scratch, the SDD ledger).
_SKIP = {
    ".venv",
    ".git",
    ".superpowers",
    "__pycache__",
    "node_modules",
    "wheelhouse",
    "dist",
    "build",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}


def _ours(paths):
    for path in paths:
        if not any(part in _SKIP for part in path.parts):
            yield path


def test_python_version_pins_an_exact_patch():
    """The canonical source is a full X.Y.Z, so the interpreter is reproducible."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", CANONICAL), f".python-version is {CANONICAL!r}, not X.Y.Z"


def test_every_dockerfile_pins_the_canonical_patch():
    """`FROM python:<tag>-slim` in every Dockerfile equals the canonical full patch."""
    checked = []
    for dockerfile in _ours(ROOT.rglob("Dockerfile*")):
        for tag in re.findall(r"FROM python:(\S+)-slim", dockerfile.read_text()):
            checked.append(dockerfile)
            assert tag == CANONICAL, f"{dockerfile}: python:{tag}-slim != python:{CANONICAL}-slim"
    assert checked, "no `FROM python:<tag>-slim` found — add the pattern if the base moved"


def test_every_pyproject_agrees_on_major_minor():
    """requires-python floor, mypy python_version and trove classifiers match X.Y."""
    checked = []
    for pyproject in _ours(ROOT.rglob("pyproject.toml")):
        text = pyproject.read_text()
        for floor in re.findall(r'requires-python\s*=\s*">=\s*([\d.]+)\s*"', text):
            checked.append(pyproject)
            assert floor == MAJOR_MINOR, f"{pyproject}: requires-python >= {floor} != {MAJOR_MINOR}"
        for version in re.findall(r'python_version\s*=\s*"([\d.]+)"', text):
            assert version == MAJOR_MINOR, f"{pyproject}: mypy python_version {version} != {MAJOR_MINOR}"
        # Only major.minor classifiers (`:: 3.14`); a bare `:: 3` is ignored.
        for classifier in re.findall(r"Programming Language :: Python :: (\d+\.\d+)", text):
            assert classifier == MAJOR_MINOR, f"{pyproject}: classifier {classifier} != {MAJOR_MINOR}"
    assert checked, "no pyproject.toml with requires-python found"


def test_every_workflow_setup_python_agrees_on_major_minor():
    """`actions/setup-python` inputs across all workflows match X.Y (patch allowed)."""
    workflows = ROOT / ".github" / "workflows"
    for workflow in [*workflows.glob("*.yml"), *workflows.glob("*.yaml")]:
        for version in re.findall(r'python-version:\s*"([\d.]+)"', workflow.read_text()):
            got = ".".join(version.split(".")[:2])
            assert got == MAJOR_MINOR, f"{workflow}: setup-python {version} != {MAJOR_MINOR}"
