"""scripts/check_version_bumps.py must compare against the last RELEASE tag.

A `-test` rehearsal tag is not a release. If it counted as the previous tag, a
package changed without a version bump, then rehearsed, would diff clean against
the rehearsal tag; the real tag would pass the guard, `skip-existing` would
skip the unchanged version on PyPI, and the release would ship the OLD bytes.
"""
import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_version_bumps", ROOT / "scripts" / "check_version_bumps.py")
assert _spec and _spec.loader
bumps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bumps)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo, check=True, capture_output=True,
    )


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)


def test_a_test_tag_is_never_the_previous_release(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "a")
    _git(tmp_path, "tag", "v1.0.0")
    _commit(tmp_path, "b")
    _git(tmp_path, "tag", "v1.1.0-test1")
    _commit(tmp_path, "c")  # the release commit: HEAD
    monkeypatch.chdir(tmp_path)
    assert bumps._prev_tag() == "v1.0.0"


def test_the_previous_release_tag_is_found_when_there_is_no_test_tag(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "a")
    _git(tmp_path, "tag", "v1.0.0")
    _commit(tmp_path, "b")
    monkeypatch.chdir(tmp_path)
    assert bumps._prev_tag() == "v1.0.0"


# The wheel of each sub-package ships only its package directory, so a change to
# the package's tests/ folder must not ask for a version bump, while a change to
# the package directory must.
_PACKAGE_DIRS = {
    "continuo-python-runtime": (".", "continuo_python_runtime"),
    "continuo-engine-contract": ("contract", "continuo_engine_contract"),
    "continuo-postgres-adapter": ("adapters/postgres", "continuo_postgres_adapter"),
    "continuo-trino-adapter": ("adapters/trino", "continuo_trino_adapter"),
    "continuo-duckdb-adapter": ("adapters/duckdb", "continuo_duckdb_adapter"),
}


def _write(repo: Path, rel: str, text: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _released_repo(repo: Path) -> None:
    """A repo laid out like the real one, with every package at 1.0.0 and tagged v1.0.0."""
    _git(repo, "init", "-q")
    for dist, (root, package) in _PACKAGE_DIRS.items():
        base = "" if root == "." else f"{root}/"
        _write(repo, f"{base}pyproject.toml", f'[project]\nname = "{dist}"\nversion = "1.0.0"\n')
        _write(repo, f"{base}{package}/__init__.py", "")
        _write(repo, f"{base}tests/test_x.py", "")
    _commit_all(repo, "release 1.0.0")
    _git(repo, "tag", "v1.0.0")


def _check(repo: Path, monkeypatch, capsys) -> tuple[int, str]:
    """Run the guard on a release commit that sits after the v1.0.0 tag."""
    monkeypatch.chdir(repo)
    code = bumps.main()
    return code, capsys.readouterr().err


def test_a_change_only_under_an_adapters_tests_needs_no_bump(tmp_path, monkeypatch, capsys):
    _released_repo(tmp_path)
    _write(tmp_path, "adapters/postgres/tests/test_x.py", "def test_new(): ...\n")
    _commit_all(tmp_path, "test change")
    code, err = _check(tmp_path, monkeypatch, capsys)
    assert code == 0, err


def test_a_change_under_an_adapters_package_needs_a_bump(tmp_path, monkeypatch, capsys):
    _released_repo(tmp_path)
    _write(tmp_path, "adapters/postgres/continuo_postgres_adapter/__init__.py", "x = 1\n")
    _commit_all(tmp_path, "source change")
    code, err = _check(tmp_path, monkeypatch, capsys)
    assert code == 1
    assert "continuo-postgres-adapter" in err
    assert "continuo-trino-adapter" not in err
    assert "(exclude)" not in err
    assert "adapters/postgres changed outside tests/" in err


def test_a_change_only_under_the_contracts_tests_needs_no_bump(tmp_path, monkeypatch, capsys):
    _released_repo(tmp_path)
    _write(tmp_path, "contract/tests/test_x.py", "def test_new(): ...\n")
    _commit_all(tmp_path, "test change")
    code, err = _check(tmp_path, monkeypatch, capsys)
    assert code == 0, err


def test_a_change_under_the_contract_package_needs_a_bump(tmp_path, monkeypatch, capsys):
    _released_repo(tmp_path)
    _write(tmp_path, "contract/continuo_engine_contract/__init__.py", "x = 1\n")
    _commit_all(tmp_path, "source change")
    code, err = _check(tmp_path, monkeypatch, capsys)
    assert code == 1
    assert "continuo-engine-contract" in err


def test_a_change_to_an_adapters_pyproject_needs_a_bump(tmp_path, monkeypatch, capsys):
    _released_repo(tmp_path)
    _write(
        tmp_path, "adapters/trino/pyproject.toml",
        '[project]\nname = "continuo-trino-adapter"\nversion = "1.0.0"\ndescription = "d"\n',
    )
    _commit_all(tmp_path, "metadata change")
    code, err = _check(tmp_path, monkeypatch, capsys)
    assert code == 1
    assert "continuo-trino-adapter" in err


def test_a_bumped_version_passes_whatever_changed(tmp_path, monkeypatch, capsys):
    _released_repo(tmp_path)
    _write(tmp_path, "adapters/duckdb/continuo_duckdb_adapter/__init__.py", "x = 1\n")
    _write(tmp_path, "adapters/duckdb/pyproject.toml",
           '[project]\nname = "continuo-duckdb-adapter"\nversion = "1.1.0"\n')
    _commit_all(tmp_path, "source change with a bump")
    code, err = _check(tmp_path, monkeypatch, capsys)
    assert code == 0, err
