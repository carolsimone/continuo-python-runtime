"""The contract version is pinned in five places that must agree: the three
package pyprojects (uv-resolved) and the two engine Dockerfiles' pip install
lines (image-resolved). A drift ships images against a different contract
than the packages were tested with — the image build fails fast on the
conflict, but far from the edit that caused it; this test names the rule at
the source.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PIN_SITES = [
    "pyproject.toml",
    "python-runtime-postgres/pyproject.toml",
    "python-runtime-trino/pyproject.toml",
    "Dockerfile.postgres",
    "Dockerfile.trino",
]

_PIN = re.compile(r"continuo-validation-contract==([0-9][0-9a-z.]*)")


def test_every_contract_pin_site_agrees_on_one_version():
    versions = {}
    for site in PIN_SITES:
        match = _PIN.search((ROOT / site).read_text())
        assert match, f"no continuo-validation-contract pin found in {site}"
        versions[site] = match.group(1)
    assert len(set(versions.values())) == 1, f"contract pin drift: {versions}"
