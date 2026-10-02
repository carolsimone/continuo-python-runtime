"""Test the domain-repo template."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from continuo_python_runtime.cli import main

TEMPLATE = Path(__file__).parent.parent / "template"


def test_template_passes_lint_validate_merge(tmp_path):
    """Template must pass lint, validate, and merge as-is."""
    assert main(["lint", str(TEMPLATE / "scripts")]) == 0
    assert main(["validate", str(TEMPLATE / "contracts")]) == 0
    out = tmp_path / "contract.yaml"
    assert main(["merge", str(TEMPLATE / "contracts"), "--service", "example",
                 "--repo-root", str(TEMPLATE), "--out", str(out)]) == 0
    assert out.exists()


def test_template_passes_hash(capsys):
    """Template must also pass the hash subcommand: one tab-separated sha256
    line per contract node — the sql-node example and the csv-node example."""
    assert main(["hash", str(TEMPLATE / "contracts"), "--repo-root", str(TEMPLATE)]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line]
    assert len(lines) == 2
    by_relation = {}
    for line in lines:
        relation, hash_value = line.split("\t")
        assert hash_value.startswith("sha256:")
        by_relation[relation] = hash_value
    assert set(by_relation) == {"analytics.example", "analytics.example_csv"}


def test_template_demonstrates_multiple_named_reads():
    """The example must declare more than one read, and use each by name.

    Node authors copy this file verbatim, so a single-entry `reads:` map reads
    as a limit of the runtime rather than as the minimal case.
    """
    doc = yaml.safe_load((TEMPLATE / "contracts" / "example.yml").read_text())
    reads = doc["nodes"][0]["reads"]
    assert len(reads) >= 2, "template must show the named-read map with 2+ entries"

    script = (TEMPLATE / "scripts" / "example.py").read_text()
    for name in reads:
        assert f'ctx.read("{name}")' in script, f"declared read {name!r} unused by the script"


def test_release_workflow_cancels_superseded_main_runs():
    """Only the newest main-branch release may finish publishing."""
    workflow = yaml.safe_load(
        (TEMPLATE / ".github" / "workflows" / "release.yml").read_text()
    )

    assert workflow["concurrency"] == {
        "group": "release",
        "cancel-in-progress": True,
    }


def _release_job():
    workflow = yaml.safe_load(
        (TEMPLATE / ".github" / "workflows" / "release.yml").read_text()
    )
    return workflow, workflow["jobs"]["release"]


def _step(job, name):
    return next(step for step in job["steps"] if step.get("name") == name)


def test_release_workflow_uses_the_public_release_api():
    """The release call is the authenticated public API, bounded and token-safe."""
    workflow, job = _release_job()
    names = [step.get("name") for step in job["steps"]]
    submit = _step(job, "Submit release to continuo")["run"]

    assert job["permissions"]["id-token"] == "write"
    assert isinstance(job["timeout-minutes"], int)
    assert workflow["env"]["RELEASE_ENDPOINT"] == "${{ vars.RELEASE_ENDPOINT }}"
    # Submit comes after the image build and the contract upload.
    assert names.index("Submit release to continuo") > names.index("Upload contract artifact")
    assert "/api/v1/releases" in submit
    assert "audience=" in submit
    assert "terminal" in submit
    assert "seq 1 90" in submit  # a bounded poll
    # The bare, unauthenticated `${{ vars.RELEASE_ENDPOINT }}/releases` call is gone.
    assert "/releases\"" not in (TEMPLATE / ".github" / "workflows" / "release.yml").read_text()
    # The body is built with jq, and the token is only ever sent as a header.
    assert "jq -n" in submit
    assert "echo \"$token" not in submit and "ACTIONS_ID_TOKEN_REQUEST_TOKEN\" >&2" not in submit


@pytest.mark.parametrize(
    ("endpoint", "ok"),
    [
        ("", False),
        ("continuo.example.com", False),
        ("https://continuo.example.com/api", False),
        ("https://continuo.example.com?x=1", False),
        ("https://continuo.example.com", True),
        ("https://continuo.example.com/", True),
        ("http://localhost:8090", True),
    ],
)
def test_release_workflow_fails_closed_on_a_bad_endpoint(tmp_path, endpoint, ok):
    """The first step rejects an unset or malformed RELEASE_ENDPOINT before any build."""
    _, job = _release_job()
    assert job["steps"][0]["name"] == "Check release endpoint"
    github_env = tmp_path / "github_env"
    github_env.touch()
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", job["steps"][0]["run"]],
        env={"PATH": os.environ["PATH"], "RELEASE_ENDPOINT": endpoint, "GITHUB_ENV": str(github_env)},
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is ok, result.stdout + result.stderr
    if ok:
        assert github_env.read_text() == f"CONTINUO_ORIGIN={endpoint.rstrip('/')}\n"
    else:
        assert "RELEASE_ENDPOINT" in result.stdout


def test_readme_and_template_name_images_the_publisher_emits():
    """Engine-selection examples must name images the publisher actually pushes.

    publish-pypi.yml pushes ``continuo-python-runtime-<engine>:<tag>``: the engine is
    part of the image NAME and the tag is the bare version, which is what lets
    Continuo's chart pin ``<name>:vX.Y.Z@sha256:<digest>``. The version is read
    from the root pyproject so neither the README nor the template Dockerfile
    can drift past a version bump unnoticed.
    """
    import tomllib

    root = TEMPLATE.parent
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    readme = (root / "README.md").read_text()
    dockerfile = (TEMPLATE / "Dockerfile").read_text()

    for engine in ("postgres", "trino"):
        assert f"continuo-python-runtime-{engine}:v{version}" in readme
    assert f"continuo-python-runtime-postgres:v{version}" in dockerfile
