"""Tests for ``installed_packages`` and the ``packages`` subcommand.

The distributions the tests add are real ``*.dist-info`` folders on ``sys.path``,
so ``importlib.metadata`` finds them exactly as it finds an installed package.
"""

import json
import logging
from importlib import metadata
from pathlib import Path

import pytest

from continuo_python_runtime.cli import main
from continuo_python_runtime.packages import installed_packages


def write_dist(root: Path, folder: str, *, name: str | None, version: str | None) -> Path:
    """Write ``root/<folder>.dist-info`` with a METADATA file naming *name* and *version*."""
    dist_info = root / f"{folder}.dist-info"
    dist_info.mkdir(parents=True)
    lines = ["Metadata-Version: 2.1"]
    if name is not None:
        lines.append(f"Name: {name}")
    if version is not None:
        lines.append(f"Version: {version}")
    (dist_info / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dist_info


def test_packages_lists_this_environment():
    packages = installed_packages()

    assert packages["continuo-python-runtime"] == metadata.version("continuo-python-runtime")
    assert packages["continuo-contracts"] == "0.4.1"
    assert packages["pyarrow"] == metadata.version("pyarrow")


@pytest.mark.parametrize(
    ("raw_name", "normalised"),
    [
        ("Foo_Bar.baz", "foo-bar-baz"),
        ("A--B__c..D", "a-b-c-d"),
        ("Mixed-_.Run", "mixed-run"),
        ("already-normal", "already-normal"),
    ],
)
def test_names_are_normalised(tmp_path, monkeypatch, raw_name, normalised):
    write_dist(tmp_path, "fake_dist", name=raw_name, version="1.2.3")
    monkeypatch.syspath_prepend(str(tmp_path))

    packages = installed_packages()

    assert packages[normalised] == "1.2.3"
    assert raw_name not in packages or raw_name == normalised


@pytest.mark.parametrize("first", ["a", "b"])
def test_the_version_python_resolves_wins_when_a_name_appears_twice(tmp_path, monkeypatch, first):
    """Two distributions of one name: the one earlier on ``sys.path`` is the one ``import`` uses."""
    versions = {"a": "1.0.0", "b": "2.0.0"}
    for key, version in versions.items():
        # The two folders spell the name differently; both normalise to `twice-named`.
        spelling = "Twice_Named" if key == "a" else "twice.named"
        write_dist(tmp_path / key, f"{spelling}-{version}", name=spelling, version=version)
    second = "b" if first == "a" else "a"
    monkeypatch.syspath_prepend(str(tmp_path / second))
    monkeypatch.syspath_prepend(str(tmp_path / first))

    packages = installed_packages()

    assert metadata.version("twice-named") == versions[first]
    assert packages["twice-named"] == versions[first]


def test_the_first_one_found_wins_when_python_cannot_resolve_a_duplicated_name(tmp_path, monkeypatch, caplog):
    """A folder that does not carry its distribution's name is not found by name, so there is nothing to resolve."""
    write_dist(tmp_path / "a", "oddly-1.0", name="odd-twin", version="1.0.0")
    write_dist(tmp_path / "b", "oddly-2.0", name="Odd_Twin", version="2.0.0")
    monkeypatch.syspath_prepend(str(tmp_path / "b"))
    monkeypatch.syspath_prepend(str(tmp_path / "a"))
    with pytest.raises(metadata.PackageNotFoundError):
        metadata.version("odd-twin")

    with caplog.at_level(logging.WARNING):
        packages = installed_packages()

    assert packages["odd-twin"] == "1.0.0"
    assert any("odd-twin appears twice" in record.getMessage() for record in caplog.records)


def test_a_distribution_without_a_name_or_a_version_is_left_out(tmp_path, monkeypatch, caplog):
    before = installed_packages()
    write_dist(tmp_path, "nameless-1.0", name=None, version="1.0")
    write_dist(tmp_path, "versionless-1.0", name="versionless", version=None)
    write_dist(tmp_path, "whole-1.0", name="whole", version="1.0")
    monkeypatch.syspath_prepend(str(tmp_path))

    with caplog.at_level(logging.WARNING):
        after = installed_packages()

    assert after == {**before, "whole": "1.0"}
    messages = [record.getMessage() for record in caplog.records]
    assert [m for m in messages if "name None and version '1.0'" in m]
    assert [m for m in messages if "name 'versionless' and version None" in m]
    assert all(str(tmp_path) in m for m in messages if "skipping" in m)


def test_the_command_prints_only_json(capsys):
    rc = main(["packages"])

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == json.dumps(installed_packages(), sort_keys=True) + "\n"
    parsed = json.loads(captured.out)
    assert isinstance(parsed, dict)
    assert parsed["continuo-contracts"] == "0.4.1"
