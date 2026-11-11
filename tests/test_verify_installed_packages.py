"""scripts/verify_installed_packages.py: the first-party package check."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_installed_packages.py"

_spec = importlib.util.spec_from_file_location("verify_installed_packages", SCRIPT)
assert _spec and _spec.loader
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)


class _Dist:
    def __init__(self, name):
        self.metadata = {"Name": name}


FIRST_PARTY = ["continuo-engine-contract", "continuo-python-runtime", "continuo-postgres-adapter"]


def test_the_runtimes_hashing_library_is_not_a_first_party_package(monkeypatch):
    names = FIRST_PARTY + ["continuo-contracts", "pyarrow"]
    monkeypatch.setattr(verify.metadata, "distributions", lambda: [_Dist(n) for n in names])
    verify.check_only_expected_first_party("postgres")


def test_a_second_adapter_still_fails_the_first_party_check(monkeypatch):
    names = FIRST_PARTY + ["continuo-contracts", "continuo-trino-adapter"]
    monkeypatch.setattr(verify.metadata, "distributions", lambda: [_Dist(n) for n in names])
    with pytest.raises(verify.Failure, match="continuo-trino-adapter"):
        verify.check_only_expected_first_party("postgres")
