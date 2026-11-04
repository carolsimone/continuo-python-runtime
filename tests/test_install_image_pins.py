"""scripts/install_image_pins.py: an engine image's pip installs at build time.

A release image is built from the pins right after they are uploaded, and the
index can still answer the build with a page from before the upload: the
runner's probe saw a fresh release, then the image build's own pip, ten
seconds later, was told the version did not exist. The script retries an
index install while pip says a pin is not served yet, and fails any other
error at once.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install_image_pins.py"

_spec = importlib.util.spec_from_file_location("install_image_pins", SCRIPT)
assert _spec and _spec.loader
pins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pins)

PIP_INSTALL = [sys.executable, "-m", "pip", "install", "--no-cache-dir"]
# pip's own words when the index does not list the pinned version (verbatim
# from a release build that ran seconds after the upload).
NOT_SERVED_YET = (
    "ERROR: Could not find a version that satisfies the requirement continuo-python-runtime==0.11.0 "
    "(from versions: 0.2.0, 0.3.0, 0.3.1, 0.4.0, 0.4.1, 0.5.0, 0.6.0, 0.7.0, 0.8.0, 0.9.0, 0.10.0)\n"
    "ERROR: No matching distribution found for continuo-python-runtime==0.11.0\n"
)
OTHER_FAILURE = "ERROR: Could not install packages due to an OSError: [Errno 28] No space left on device\n"


class Pip:
    """subprocess.run replacement: answers each pip call with the next scripted stderr (None = success)."""

    def __init__(self, failures):
        self.failures = list(failures)
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        failure = self.failures.pop(0) if self.failures else None
        return subprocess.CompletedProcess(argv, 1 if failure else 0, stdout="", stderr=failure or "")


def _wheelhouse(tmp_path, adapter="postgres"):
    house = tmp_path / "wheelhouse"
    house.mkdir()
    for name in ("continuo_engine_contract-0.9.0", "continuo_python_runtime-0.11.0", f"continuo_{adapter}_adapter-0.3.2"):
        (house / f"{name}-py3-none-any.whl").write_bytes(b"")
    return house


def test_a_pin_the_index_does_not_serve_yet_is_retried_until_it_does():
    pip = Pip([NOT_SERVED_YET, NOT_SERVED_YET, None])
    sleeps: list[float] = []

    pins.install_pins("pypi", "postgres", "/tmp/req.txt", "/tmp/wheelhouse", run=pip, sleep=sleeps.append)

    assert pip.calls == [PIP_INSTALL + ["-r", "/tmp/req.txt"]] * 3
    assert sleeps == [20.0, 20.0]


def test_a_pin_the_index_never_serves_fails_after_thirty_attempts():
    pip = Pip([NOT_SERVED_YET] * 100)
    sleeps: list[float] = []

    with pytest.raises(pins.InstallError, match="No matching distribution found"):
        pins.install_pins("pypi", "postgres", "/tmp/req.txt", "/tmp/wheelhouse", run=pip, sleep=sleeps.append)

    assert len(pip.calls) == 30 and len(sleeps) == 29


def test_any_other_pip_failure_is_raised_at_once():
    pip = Pip([OTHER_FAILURE, None])
    sleeps: list[float] = []

    with pytest.raises(pins.InstallError, match="No space left on device"):
        pins.install_pins("pypi", "postgres", "/tmp/req.txt", "/tmp/wheelhouse", run=pip, sleep=sleeps.append)

    assert len(pip.calls) == 1 and sleeps == []


def test_a_rehearsal_takes_only_first_party_pins_from_testpypi_and_retries_both_installs():
    pip = Pip([NOT_SERVED_YET, None, NOT_SERVED_YET, None])
    sleeps: list[float] = []

    pins.install_pins("testpypi", "trino", "/tmp/req.txt", "/tmp/wheelhouse", run=pip, sleep=sleeps.append)

    # First-party only from TestPyPI (it hosts junk copies of real names): no
    # dependencies, no sdists (an sdist would pull its build backend from it
    # too), and never an extra index. Third parties then come from PyPI.
    testpypi = PIP_INSTALL + ["--no-deps", "--only-binary=:all:", "--index-url", "https://test.pypi.org/simple/",
                              "-r", "/tmp/req.txt"]
    pypi = PIP_INSTALL + ["-r", "/tmp/req.txt"]
    assert pip.calls == [testpypi, testpypi, pypi, pypi]
    assert not any("--extra-index-url" in arg for call in pip.calls for arg in call)
    assert sleeps == [20.0, 20.0]


def test_the_wheelhouse_installs_this_runs_wheels_without_an_index(tmp_path):
    house = _wheelhouse(tmp_path, "trino")
    pip = Pip([])

    pins.install_pins("wheelhouse", "trino", "/tmp/req.txt", str(house), run=pip, sleep=lambda _s: None)

    assert pip.calls == [
        PIP_INSTALL + ["--no-index", "--no-deps",
                       str(house / "continuo_engine_contract-0.9.0-py3-none-any.whl"),
                       str(house / "continuo_python_runtime-0.11.0-py3-none-any.whl"),
                       str(house / "continuo_trino_adapter-0.3.2-py3-none-any.whl")],
        PIP_INSTALL + ["-r", "/tmp/req.txt"],
    ]


def test_a_wheelhouse_install_that_fails_is_not_retried(tmp_path):
    house = _wheelhouse(tmp_path)
    pip = Pip([NOT_SERVED_YET, None])
    sleeps: list[float] = []

    with pytest.raises(pins.InstallError):
        pins.install_pins("wheelhouse", "postgres", "/tmp/req.txt", str(house), run=pip, sleep=sleeps.append)

    assert len(pip.calls) == 1 and sleeps == []


def test_a_wheelhouse_missing_a_wheel_names_it(tmp_path):
    house = _wheelhouse(tmp_path)
    (house / "continuo_postgres_adapter-0.3.2-py3-none-any.whl").unlink()

    with pytest.raises(pins.InstallError, match="continuo_postgres_adapter"):
        pins.install_pins("wheelhouse", "postgres", "/tmp/req.txt", str(house), run=Pip([]), sleep=lambda _s: None)


def test_an_unknown_wheel_source_is_refused():
    with pytest.raises(pins.InstallError, match="pypi|testpypi|wheelhouse"):
        pins.install_pins("conda", "postgres", "/tmp/req.txt", "/tmp/wheelhouse", run=Pip([]), sleep=lambda _s: None)


def test_main_exits_non_zero_and_logs_the_failure(caplog, monkeypatch):
    monkeypatch.setattr(pins.subprocess, "run", Pip([OTHER_FAILURE]))
    rc = pins.main(["--source", "pypi", "--adapter", "postgres",
                    "--requirements", "/tmp/req.txt", "--wheelhouse", "/tmp/wheelhouse"])
    assert rc == 1
    assert "No space left on device" in caplog.text
