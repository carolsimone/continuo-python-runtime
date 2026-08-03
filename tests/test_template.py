"""Test the domain-repo template."""

from pathlib import Path

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
    """Template must also pass the hash subcommand, one tab-separated sha256 line."""
    assert main(["hash", str(TEMPLATE / "contracts"), "--repo-root", str(TEMPLATE)]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line]
    assert len(lines) == 1
    relation, hash_value = lines[0].split("\t")
    assert relation == "analytics.example"
    assert hash_value.startswith("sha256:")


def test_release_workflow_cancels_superseded_main_runs():
    """Only the newest main-branch release may finish publishing."""
    workflow = yaml.safe_load(
        (TEMPLATE / ".github" / "workflows" / "release.yml").read_text()
    )

    assert workflow["concurrency"] == {
        "group": "release",
        "cancel-in-progress": True,
    }


def test_readme_uses_published_v_prefixed_image_tags():
    """Engine-selection examples must name tags emitted by the publisher."""
    readme = (TEMPLATE.parent / "README.md").read_text()

    assert "continuo-python-runtime:v0.1.0-postgres" in readme
    assert "continuo-python-runtime:v0.1.0-trino" in readme
