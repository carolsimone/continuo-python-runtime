"""Verify an engine's packages as an installer would see them (stdlib only).

Run by the package gate (.github/workflows/verify-packages.yml) inside a clean
venv that holds ONLY the built wheels of continuo-engine-contract,
continuo-python-runtime and one adapter, with `python -I` from a directory that
is not the repo. It proves what a pip install of the upload would give you:

  * the installed versions are the versions of the wheels in dist/ (not a
    same-name release from an index);
  * exactly one engine adapter is installed and the contract's own
    `discover_adapter()` resolves it to the expected class;
  * that class lives in site-packages, not in a source checkout;
  * `required_env()` is the expected list;
  * the `continuo-runtime` console script starts.

    python -I verify_installed_packages.py --engine duckdb --dist-dir dist
"""
from __future__ import annotations

import argparse
import importlib
import logging
import subprocess
import sys
from importlib import metadata
from pathlib import Path

logger = logging.getLogger("verify_installed_packages")

# engine -> (adapter dist, adapter class path, entry-point name, required_env())
ENGINES: dict[str, tuple[str, str, str, list[str]]] = {
    "postgres": (
        "continuo-postgres-adapter",
        "continuo_postgres_adapter.adapter:PostgresAdapter",
        "postgres",
        ["POSTGRES_HOST", "POSTGRES_DB", "POSTGRES_USER"],
    ),
    "trino": (
        "continuo-trino-adapter",
        "continuo_trino_adapter.adapter:TrinoAdapter",
        "trino",
        ["TRINO_HOST", "TRINO_CATALOG"],
    ),
    "duckdb": (
        "continuo-duckdb-adapter",
        "continuo_duckdb_adapter.adapter:DuckDBAdapter",
        "duckdb",
        ["DUCKDB_CATALOG_HOST", "DUCKDB_CATALOG_DB", "DUCKDB_CATALOG_USER", "DUCKDB_DATA_PATH"],
    ),
}
FIRST_PARTY_PREFIX = "continuo-"


class Failure(Exception):
    pass


def _norm(name: str) -> str:
    return name.lower().replace("_", "-")


def wheel_versions(dist_dir: Path) -> dict[str, str]:
    """dist name -> version, from the wheel file names in *dist_dir*."""
    found = {}
    for wheel in dist_dir.glob("*.whl"):
        name, version, *_ = wheel.name.split("-")
        found[_norm(name)] = version
    return found


def check_versions(engine: str, dist_dir: Path) -> None:
    adapter_dist = ENGINES[engine][0]
    expected = wheel_versions(dist_dir)
    for dist in ("continuo-engine-contract", "continuo-python-runtime", adapter_dist):
        if dist not in expected:
            raise Failure(f"no wheel for {dist} in {dist_dir}")
        installed = metadata.version(dist)
        if installed != expected[dist]:
            raise Failure(f"{dist}: installed {installed} but dist/ holds {expected[dist]}")
        logger.info("ok  %s==%s is the dist/ wheel", dist, installed)


def check_only_expected_first_party(engine: str) -> None:
    adapter_dist = ENGINES[engine][0]
    installed = {
        _norm(d.metadata["Name"])
        for d in metadata.distributions()
        if _norm(d.metadata["Name"]).startswith(FIRST_PARTY_PREFIX)
    }
    expected = {"continuo-engine-contract", "continuo-python-runtime", adapter_dist}
    if installed != expected:
        raise Failure(f"first-party distributions {sorted(installed)}, expected {sorted(expected)}")
    logger.info("ok  first-party set is exactly %s", sorted(expected))


def check_entry_point(engine: str) -> type:
    _, class_path, ep_name, _ = ENGINES[engine]
    from continuo_engine_contract.port import discover_adapter

    name, cls = discover_adapter()
    module_name, _, attr = class_path.partition(":")
    if name != ep_name:
        raise Failure(f"discover_adapter() returned engine {name!r}, expected {ep_name!r}")
    if cls.__module__ != module_name or cls.__name__ != attr:
        raise Failure(f"adapter resolved to {cls.__module__}.{cls.__name__}, expected {class_path}")
    logger.info("ok  discover_adapter() -> %s (%s.%s)", name, cls.__module__, cls.__name__)
    return cls


def check_installed_not_checkout(cls: type) -> None:
    for module in (sys.modules[cls.__module__], importlib.import_module("continuo_python_runtime")):
        path = Path(module.__file__ or "")
        if "site-packages" not in path.parts:
            raise Failure(f"{module.__name__} imported from {path}, not from site-packages")
    logger.info("ok  imported from site-packages")


def check_required_env(engine: str, cls: type) -> None:
    expected = ENGINES[engine][3]
    actual = cls.required_env()  # type: ignore[attr-defined]
    if actual != expected:
        raise Failure(f"required_env() is {actual}, expected {expected}")
    logger.info("ok  required_env() == %s", expected)


def check_console_script() -> None:
    script = Path(sys.executable).parent / "continuo-runtime"
    proc = subprocess.run([str(script), "--help"], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 or "usage" not in (proc.stdout + proc.stderr).lower():
        raise Failure(f"`continuo-runtime --help` exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    logger.info("ok  continuo-runtime --help")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--engine", choices=sorted(ENGINES), required=True)
    ap.add_argument("--dist-dir", type=Path, required=True)
    args = ap.parse_args(argv)
    try:
        check_versions(args.engine, args.dist_dir)
        check_only_expected_first_party(args.engine)
        cls = check_entry_point(args.engine)
        check_installed_not_checkout(cls)
        check_required_env(args.engine, cls)
        check_console_script()
    except (Failure, metadata.PackageNotFoundError, ImportError) as exc:
        logger.error("FAIL %s: %s", type(exc).__name__, exc)
        return 1
    logger.info("all package checks passed for %s", args.engine)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
