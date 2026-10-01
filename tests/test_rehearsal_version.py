"""scripts/apply_rehearsal_version.py: CI-only rewrite to a unique PEP 440 dev version.

A -test tag or a dry run uploads to TestPyPI, which is immutable, so every run
needs versions nobody has uploaded before. The rewrite must leave the repo
internally consistent: the sync and contract-pin guard tests must still pass
on the rewritten tree, otherwise the rehearsal would test a different shape
than a real release.
"""
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "apply_rehearsal_version.py"

_spec = importlib.util.spec_from_file_location("apply_rehearsal_version", SCRIPT)
assert _spec and _spec.loader
rehearsal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rehearsal)

_TREE = [
    "pyproject.toml",
    "contract/pyproject.toml",
    "adapters/postgres/pyproject.toml",
    "adapters/trino/pyproject.toml",
    "adapters/duckdb/pyproject.toml",
    "image-requirements-postgres.txt",
    "image-requirements-trino.txt",
    "image-requirements-duckdb.txt",
]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for rel in _TREE:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dest)
    return tmp_path


def test_every_package_version_gets_the_suffix(tree):
    before = {rel: rehearsal.read_version(tree / rel) for rel in rehearsal.PACKAGE_PYPROJECTS}
    rehearsal.apply(tree, ".dev1201")
    for rel, old in before.items():
        assert rehearsal.read_version(tree / rel) == f"{old}.dev1201"


def test_contract_pins_follow_the_contract_version(tree):
    rehearsal.apply(tree, ".dev1201")
    contract = rehearsal.read_version(tree / "contract/pyproject.toml")
    for rel in rehearsal.CONTRACT_PIN_SITES:
        assert f'"continuo-engine-contract=={contract}"' in (tree / rel).read_text()


def test_image_requirements_follow_the_package_versions(tree):
    rehearsal.apply(tree, ".dev1201")
    runtime = rehearsal.read_version(tree / "pyproject.toml")
    pg = rehearsal.read_version(tree / "adapters/postgres/pyproject.toml")
    pins = (tree / "image-requirements-postgres.txt").read_text()
    assert f"continuo-python-runtime=={runtime}" in pins
    assert f"continuo-postgres-adapter=={pg}" in pins


def test_third_party_pins_are_untouched(tree):
    rehearsal.apply(tree, ".dev1201")
    text = (tree / "pyproject.toml").read_text()
    assert '"boto3==' in text and "boto3==1.43.85" in text


def test_the_rewritten_tree_still_passes_the_consistency_guards(tree):
    """The guard tests read their files relative to their own location, so run
    them against the rewritten copy by placing them inside it."""
    rehearsal.apply(tree, ".dev1201")
    for name in ("test_image_requirements_sync.py", "test_contract_pin_consistency.py"):
        shutil.copy(ROOT / "tests" / name, tree / name)
    # The guards resolve ROOT as the parent of their directory.
    (tree / "tests").mkdir()
    for name in ("test_image_requirements_sync.py", "test_contract_pin_consistency.py"):
        shutil.move(tree / name, tree / "tests" / name)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider",
         "--import-mode=importlib"],
        cwd=tree, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("bad", ["", "dev1", ".dev", ".dev1a", ".post1", "1.dev1", ".dev1 "])
def test_a_malformed_suffix_is_rejected(tree, bad):
    with pytest.raises(ValueError):
        rehearsal.apply(tree, bad)


def test_applying_twice_is_refused(tree):
    """A second rewrite would stack suffixes (0.1.0.dev1.dev2)."""
    rehearsal.apply(tree, ".dev1")
    with pytest.raises(ValueError, match="already"):
        rehearsal.apply(tree, ".dev2")


def test_a_drifted_pin_is_reported_not_papered_over(tree):
    """If an image pin disagrees with its pyproject, rewriting would hide it."""
    path = tree / "image-requirements-trino.txt"
    path.write_text(path.read_text().replace("continuo-trino-adapter==", "continuo-trino-adapter==9."))
    with pytest.raises(ValueError, match="continuo-trino-adapter"):
        rehearsal.apply(tree, ".dev1")


def test_the_suffix_is_a_valid_pep440_dev_release():
    assert rehearsal.suffix_for(run_number=12, run_attempt=1) == ".dev1201"
    assert rehearsal.suffix_for(run_number=12, run_attempt=2) == ".dev1202"
    assert rehearsal.suffix_for(run_number=13, run_attempt=1) > rehearsal.suffix_for(12, 9)


def test_cli_prints_only_the_suffix_on_stdout(tree, capsys):
    """The workflow captures stdout as the suffix; diagnostics must stay on stderr."""
    assert rehearsal.main(["--root", str(tree), "--run-number", "12", "--run-attempt", "1"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ".dev1201\n"


def test_cli_can_reapply_an_explicit_suffix(tree, capsys):
    """Image jobs re-apply the suffix `prepare` computed to their own checkout."""
    assert rehearsal.main(["--root", str(tree), "--suffix", ".dev1201"]) == 0
    assert capsys.readouterr().out == ".dev1201\n"
    assert rehearsal.read_version(tree / "contract/pyproject.toml").endswith(".dev1201")


def test_cli_requires_a_suffix_source(tree):
    with pytest.raises(SystemExit):
        rehearsal.main(["--root", str(tree)])
