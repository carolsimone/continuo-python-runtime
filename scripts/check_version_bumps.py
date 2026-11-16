"""Fail if a package's source changed since the previous release tag but its
version was not bumped. skip-existing then safely carries the unchanged
packages. First release (no previous v* tag) is a no-op.

A package is only an offender when its dist NAME is also unchanged since the
previous tag: if the name changed (e.g. a rename to a new PyPI project), the
same version number is a first publish under that name, not a stale
re-publish, so no bump is required.

A change under a sub-package's own `tests/` folder needs no bump: each wheel
ships only its package directory (`[tool.hatch.build.targets.wheel] packages`),
so the tests never reach the published code. Every other path of a package
still counts: its pyproject.toml, its package directory, and the README,
LICENSE and NOTICE that reach the wheel's metadata and files. The root package
has no tests exclusion, and its paths name each of those files because its
pyproject.toml takes them from the repository root (`readme`, `license-files`).
"""
import subprocess
import sys
import tomllib

_EXCLUDE = ":(exclude)"


def _without_tests(directory):
    """A sub-package's git pathspecs: the whole directory except its tests/ folder."""
    return [directory, f"{_EXCLUDE}{directory}/tests"]


# dist name -> (pyproject path, git pathspecs whose change requires a version bump)
PACKAGES = {
    "continuo-python-runtime": (
        "pyproject.toml",
        ["pyproject.toml", "continuo_python_runtime", "README.md", "LICENSE", "NOTICE"],
    ),
    "continuo-engine-contract": ("contract/pyproject.toml", _without_tests("contract")),
    "continuo-postgres-adapter": ("adapters/postgres/pyproject.toml", _without_tests("adapters/postgres")),
    "continuo-trino-adapter": ("adapters/trino/pyproject.toml", _without_tests("adapters/trino")),
    "continuo-duckdb-adapter": ("adapters/duckdb/pyproject.toml", _without_tests("adapters/duckdb")),
}


def _run(*args):
    return subprocess.run(args, capture_output=True, text=True)


def _prev_tag():
    # `-test` tags are rehearsals, not releases: a package changed without a bump
    # would diff clean against one, and skip-existing would then ship old bytes.
    r = _run("git", "describe", "--tags", "--match", "v*", "--exclude", "*-[Tt][Ee][Ss][Tt]*",
             "--abbrev=0", "HEAD^")
    return r.stdout.strip() if r.returncode == 0 else ""


def _name_version(ref, path):
    if ref:
        r = _run("git", "show", f"{ref}:{path}")
        if r.returncode != 0:
            return None  # package did not exist at prev tag -> treat as new
        data = tomllib.loads(r.stdout)
    else:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    return data["project"]["name"], data["project"]["version"]


def main() -> int:
    prev = _prev_tag()
    if not prev:
        print("no previous v* tag; first release, nothing to check")
        return 0
    offenders = []
    for dist, (pyproj, paths) in PACKAGES.items():
        changed = _run("git", "diff", "--quiet", f"{prev}..HEAD", "--", *paths).returncode != 0
        old = _name_version(prev, pyproj)
        new = _name_version("", pyproj)
        if changed and old is not None and old[0] == new[0] and old[1] == new[1]:
            counted = ", ".join(p for p in paths if not p.startswith(_EXCLUDE))
            where = "outside tests/ " if any(p.startswith(_EXCLUDE) for p in paths) else ""
            offenders.append(
                f"{dist} ({counted} changed {where}since {prev} but version stayed {new[1]})"
            )
    if offenders:
        print("Bump the version of changed packages before tagging:\n  " + "\n  ".join(offenders),
              file=sys.stderr)
        return 1
    print(f"All packages changed since {prev} were version-bumped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
