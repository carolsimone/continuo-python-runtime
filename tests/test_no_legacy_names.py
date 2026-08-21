"""The merge removed the old validation-package names outright (spec decision 8).

This guard keeps them from creeping back in via a copy-paste from an old
branch, doc snippet, or stale pin.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LEGACY = [
    "continuo-validation-contract",
    "continuo_validation_contract",
    "continuo-validation-runner",
    "continuo_validation_runner",
    "continuo_validation.adapters",
    "continuo_runtime.adapters",
    "discover_runtime_adapter",
    "ValidationAdapter",
    "RuntimeAdapter",
]

# docs/superpowers/ holds dated design records (the 2026-07-31 python-runtime
# plan and spec, and the 2026-08-07 config-hash plan). They describe what was
# true on the day they were written; rewriting them to today's names would
# falsify the design history, so they are exempt from the sweep.
#
# CHANGELOG.md is the same category: its 0.3.0 entry names the package this
# rename replaced, because that is what actually shipped in that release.
EXEMPT_PREFIXES = ("docs/superpowers/", "CHANGELOG.md")


def test_no_legacy_validation_names_anywhere():
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    offenders = []
    for rel in tracked:
        if rel.startswith(EXEMPT_PREFIXES) or rel == "tests/test_no_legacy_names.py":
            continue
        path = ROOT / rel
        if path.suffix in {".lock"} or not path.is_file():
            continue
        text = path.read_text(errors="ignore")
        for name in LEGACY:
            if name in text:
                offenders.append(f"{rel}: {name}")
    assert not offenders, "legacy validation names found:\n" + "\n".join(offenders)
