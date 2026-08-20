"""The contract version is pinned in the three package pyprojects, and all of
them must equal the version `contract/pyproject.toml` actually declares.

`[tool.uv.sources]` redirects each pin to the in-tree workspace member, so a
stale pin is invisible during development and in the images (which install the
contract from its local path). It becomes visible only in a built
distribution's metadata, where the pin is emitted verbatim — so a drifted pin
ships a wheel demanding a contract release that was never tested with it, or
that does not exist. This test names the rule at the source instead.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SOURCE_OF_TRUTH = "contract/pyproject.toml"

PIN_SITES = [
    "pyproject.toml",
    "adapters/postgres/pyproject.toml",
    "adapters/trino/pyproject.toml",
]

_PIN = re.compile(r"continuo-engine-contract==([0-9][0-9a-z.]*)")
_VERSION = re.compile(r"^version\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)


def _declared_contract_version() -> str:
    match = _VERSION.search((ROOT / SOURCE_OF_TRUTH).read_text())
    assert match, f"no version found in {SOURCE_OF_TRUTH}"
    return match.group(1)


def test_every_contract_pin_matches_the_contract_package_version():
    expected = _declared_contract_version()
    pinned = {}
    for site in PIN_SITES:
        match = _PIN.search((ROOT / site).read_text())
        assert match, (
            f"no continuo-engine-contract=={expected} pin found in {site}; "
            "an unbounded requirement is emitted verbatim into built metadata "
            "and would resolve whatever contract release is newest"
        )
        pinned[site] = match.group(1)
    drifted = {site: v for site, v in pinned.items() if v != expected}
    assert not drifted, (
        f"contract pin drift: {SOURCE_OF_TRUTH} declares {expected}, but {drifted}"
    )
