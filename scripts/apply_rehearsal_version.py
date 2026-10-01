"""Rewrite every first-party version to a unique PEP 440 dev release (CI only).

TestPyPI is immutable, and publish-pypi.yml would otherwise skip an upload whose
version already exists, so a second rehearsal of the same pyproject versions
would silently verify the OLDER upload. A -test tag or a dry run therefore
runs this script on its checkout before building: every package, every exact
`continuo-engine-contract==` pin and every `image-requirements-*.txt` pin move
together to `<version>.dev<N>`, so the five packages, their metadata and the
image pins stay mutually consistent (tests/test_image_requirements_sync.py and
tests/test_contract_pin_consistency.py pass on the rewritten tree).

A real release tag never runs this script.

    python scripts/apply_rehearsal_version.py --run-number 12 --run-attempt 1  # prints .dev1201
    python scripts/apply_rehearsal_version.py --suffix .dev1201  # re-apply in another job
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import tomllib
from pathlib import Path

logger = logging.getLogger("apply_rehearsal_version")

PACKAGE_PYPROJECTS = (
    "pyproject.toml",
    "contract/pyproject.toml",
    "adapters/postgres/pyproject.toml",
    "adapters/trino/pyproject.toml",
    "adapters/duckdb/pyproject.toml",
)
# Every pyproject that pins continuo-engine-contract exactly.
CONTRACT_PIN_SITES = (
    "pyproject.toml",
    "adapters/postgres/pyproject.toml",
    "adapters/trino/pyproject.toml",
    "adapters/duckdb/pyproject.toml",
)
IMAGE_REQUIREMENTS = (
    "image-requirements-postgres.txt",
    "image-requirements-trino.txt",
    "image-requirements-duckdb.txt",
)
CONTRACT = "continuo-engine-contract"

_SUFFIX = re.compile(r"\.dev[0-9]+")
_VERSION_LINE = re.compile(r'^(version\s*=\s*")([^"]+)(")', re.MULTILINE)
_PIN = re.compile(r'(")' + re.escape(CONTRACT) + r'==([0-9][0-9A-Za-z.]*)(")')


def suffix_for(run_number: int, run_attempt: int) -> str:
    """`.dev<run_number><attempt, 2 digits>`: unique per run AND per re-run, and increasing."""
    if not 1 <= run_attempt <= 99:
        raise ValueError(f"run_attempt out of range: {run_attempt}")
    return f".dev{run_number * 100 + run_attempt}"


def read_version(pyproject: Path) -> str:
    with open(pyproject, "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def read_name(pyproject: Path) -> str:
    with open(pyproject, "rb") as fh:
        return tomllib.load(fh)["project"]["name"]


def apply(root: Path, suffix: str) -> None:
    """Rewrite *root* in place. Raises ValueError, writing nothing, on any inconsistency."""
    if not _SUFFIX.fullmatch(suffix):
        raise ValueError(f"suffix must look like '.dev123', got {suffix!r}")

    versions = {rel: read_version(root / rel) for rel in PACKAGE_PYPROJECTS}
    names = {rel: read_name(root / rel) for rel in PACKAGE_PYPROJECTS}
    already = [rel for rel, v in versions.items() if ".dev" in v]
    if already:
        raise ValueError(f"already a dev version, refusing to stack a suffix: {already}")
    by_name = {names[rel]: versions[rel] for rel in PACKAGE_PYPROJECTS}

    # Compute every new file body first, so a failure writes nothing.
    new_text: dict[str, str] = {}
    for rel in PACKAGE_PYPROJECTS:
        text = (root / rel).read_text()
        text, n = _VERSION_LINE.subn(lambda m: f"{m[1]}{m[2]}{suffix}{m[3]}", text, count=1)
        if n != 1:
            raise ValueError(f"{rel}: no version line found")
        new_text[rel] = text
    for rel in CONTRACT_PIN_SITES:
        text, n = _PIN.subn(
            lambda m: f"{m[1]}{CONTRACT}=={m[2]}{suffix}{m[3]}", new_text[rel]
        )
        if n < 1:
            raise ValueError(f"{rel}: no exact {CONTRACT} pin found")
        for m in _PIN.finditer(new_text[rel]):
            if m[2] != by_name[CONTRACT]:
                raise ValueError(
                    f"{rel}: pins {CONTRACT}=={m[2]} but the contract is {by_name[CONTRACT]}"
                )
        new_text[rel] = text
    for rel in IMAGE_REQUIREMENTS:
        lines = []
        for line in (root / rel).read_text().splitlines():
            name, sep, pinned = line.strip().partition("==")
            if sep and not line.lstrip().startswith("#"):
                if name not in by_name:
                    raise ValueError(f"{rel}: unexpected requirement {name!r}")
                if pinned != by_name[name]:
                    raise ValueError(
                        f"{rel}: {name}=={pinned} does not match its pyproject ({by_name[name]})"
                    )
                line = f"{name}=={pinned}{suffix}"
            lines.append(line)
        new_text[rel] = "\n".join(lines) + "\n"

    for rel, text in new_text.items():
        (root / rel).write_text(text)
        logger.info("rewrote %s", rel)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--suffix", help="an explicit suffix, e.g. one `prepare` already computed")
    source.add_argument("--run-number", type=int)
    ap.add_argument("--run-attempt", type=int)
    ap.add_argument("--root", type=Path, default=Path("."))
    args = ap.parse_args(argv)
    if args.suffix is not None:
        suffix = args.suffix
    elif args.run_attempt is None:
        ap.error("--run-number needs --run-attempt")
    else:
        suffix = suffix_for(args.run_number, args.run_attempt)
    apply(args.root, suffix)
    # The one stdout line is machine-read by the workflow (no logging here).
    print(suffix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
