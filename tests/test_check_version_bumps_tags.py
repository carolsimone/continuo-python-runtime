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
