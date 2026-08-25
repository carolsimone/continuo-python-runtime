"""The engine image installs pinned versions from image-requirements-<engine>.txt.
Guard that those pins equal the repo's own pyproject versions, so the image can
never ship a version different from what this tag publishes.
"""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _v(rel: str) -> str:
    with open(ROOT / rel, "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def _pins(rel: str) -> dict[str, str]:
    pins = {}
    for line in (ROOT / rel).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, ver = line.partition("==")
        pins[name.strip()] = ver.strip()
    return pins


def test_postgres_image_requirements_match_pyproject():
    pins = _pins("image-requirements-postgres.txt")
    assert pins["continuo-python-runtime"] == _v("pyproject.toml")
    assert pins["continuo-postgres-adapter"] == _v("adapters/postgres/pyproject.toml")


def test_trino_image_requirements_match_pyproject():
    pins = _pins("image-requirements-trino.txt")
    assert pins["continuo-python-runtime"] == _v("pyproject.toml")
    assert pins["continuo-trino-adapter"] == _v("adapters/trino/pyproject.toml")
