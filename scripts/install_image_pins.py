"""Install an engine image's pinned distributions, at image build time.

Each ``Dockerfile.<engine>`` copies this script in and runs it once, with the
image's ``WHEEL_SOURCE``:

``pypi``
    The pins of ``image-requirements-<engine>.txt`` from PyPI (a release).
``testpypi``
    The pins from TestPyPI, first-party only, then their third-party
    dependencies from PyPI (a ``-test`` rehearsal). TestPyPI hosts junk copies
    of real names, so nothing else is taken from it.
``wheelhouse``
    This run's own first-party wheels from the wheelhouse directory, without
    an index, then their third-party dependencies from PyPI (CI and pull
    requests). The wheels are installed by exact file first: a plain
    ``--find-links`` install is not enough, because pip does not prefer a
    find-links wheel over an index match of the same name and version, and
    the runtime and contract are often already on PyPI under the version the
    repository carries (a dependency bump lands between releases without a
    version bump). pip would then install the last release instead of the
    wheels this run built. The second install has only third-party
    dependencies left to resolve.

A release builds its images right after it uploads the pins, and the index can
still answer a request with a page from before the upload: two requests
seconds apart can get different answers. So an install that reads an index is
tried again while pip says a pinned version is not served yet, up to
``INDEX_ATTEMPTS`` times, ``INDEX_RETRY_SECONDS`` apart. Every other failure
is raised at once. Standard library only: it runs before anything is
installed.
"""
import argparse
import glob
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

logger = logging.getLogger("install_image_pins")

PIP_INSTALL = [sys.executable, "-m", "pip", "install", "--no-cache-dir"]
TESTPYPI = "https://test.pypi.org/simple/"
INDEX_ATTEMPTS = 30
INDEX_RETRY_SECONDS = 20.0
# What pip prints when the index lists no distribution for a pinned version.
_NOT_SERVED_YET = ("No matching distribution found", "Could not find a version that satisfies")
_FIRST_PARTY = ("continuo_engine_contract", "continuo_python_runtime")


class InstallError(Exception):
    """A pip install of the image's pins failed."""


def _wheel(wheelhouse: str, distribution: str) -> str:
    matches = sorted(glob.glob(os.path.join(wheelhouse, f"{distribution}-*.whl")))
    if len(matches) != 1:
        raise InstallError(f"expected one {distribution} wheel in {wheelhouse}, found {len(matches)}")
    return matches[0]


def _installs(source: str, adapter: str, requirements: str, wheelhouse: str) -> list[tuple[list[str], bool]]:
    """The pip installs for *source*, in order, each with whether it reads an index."""
    from_pypi = (PIP_INSTALL + ["-r", requirements], True)
    if source == "pypi":
        return [from_pypi]
    if source == "testpypi":
        first_party = PIP_INSTALL + ["--no-deps", "--only-binary=:all:", "--index-url", TESTPYPI, "-r", requirements]
        return [(first_party, True), from_pypi]
    if source == "wheelhouse":
        wheels = [_wheel(wheelhouse, name) for name in (*_FIRST_PARTY, f"continuo_{adapter}_adapter")]
        return [(PIP_INSTALL + ["--no-index", "--no-deps", *wheels], False), from_pypi]
    raise InstallError(f"unknown WHEEL_SOURCE={source!r} (pypi|testpypi|wheelhouse)")


def _install(
    argv: list[str],
    reads_index: bool,
    run: Callable[..., Any],
    sleep: Callable[[float], None],
) -> None:
    for attempt in range(1, INDEX_ATTEMPTS + 1):
        result = run(argv, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0:
            return
        sys.stderr.write(result.stderr)
        not_served_yet = any(marker in result.stderr for marker in _NOT_SERVED_YET)
        if not reads_index or not not_served_yet or attempt == INDEX_ATTEMPTS:
            raise InstallError(f"pip install failed ({result.returncode}): {result.stderr.strip()}")
        logger.warning(
            "the index does not serve a pinned version yet (attempt %d of %d), retrying in %.0f s",
            attempt, INDEX_ATTEMPTS, INDEX_RETRY_SECONDS,
        )
        sleep(INDEX_RETRY_SECONDS)


def install_pins(
    source: str,
    adapter: str,
    requirements: str,
    wheelhouse: str,
    *,
    run: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Install the image's pins from *source*, retrying an index that lags its upload.

    *run* and *sleep* default to ``subprocess.run`` and ``time.sleep``.

    Raises:
        InstallError: On an unknown *source*, a wheelhouse without exactly one
            wheel per first-party distribution, or a pip install that failed.
    """
    for argv, reads_index in _installs(source, adapter, requirements, wheelhouse):
        _install(argv, reads_index, run or subprocess.run, sleep or time.sleep)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="pypi | testpypi | wheelhouse")
    parser.add_argument("--adapter", required=True, help="the engine, e.g. postgres")
    parser.add_argument("--requirements", required=True, help="image-requirements-<engine>.txt")
    parser.add_argument("--wheelhouse", required=True, help="directory of this run's wheels")
    args = parser.parse_args(argv)
    try:
        install_pins(args.source, args.adapter, args.requirements, args.wheelhouse)
    except InstallError as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
