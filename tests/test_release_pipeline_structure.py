"""Guard: the release pipeline keeps the properties that make a release verified.

publish-pypi.yml is one graph: package gate -> upload -> candidate images
(pushed by digest, untagged) -> verify each digest -> promote. These tests parse
the workflows and fail if an edit loosens an ordering that the safety of an
irreversible PyPI upload and of a consumer-pinned image tag depends on.
"""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ENGINES = sorted(p.name for p in (ROOT / "adapters").iterdir() if (p / "tests").is_dir())
ARCHES = ["amd64", "arm64"]


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _needs(job: dict) -> set[str]:
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


RELEASE = _load("publish-pypi.yml")["jobs"]
PR = _load("images.yml")["jobs"]


def _triggers(name: str) -> dict:
    doc = _load(name)
    return doc.get("on") or doc[True]  # PyYAML reads the bare key `on` as True


@pytest.mark.parametrize(
    "jobs, job_name",
    [
        (PR, "verify-packages"), (PR, "build"), (PR, "smoke"),
        (RELEASE, "verify-packages"), (RELEASE, "preflight-build"), (RELEASE, "preflight-smoke"),
        (RELEASE, "candidate-build"), (RELEASE, "candidate-verify"),
    ],
)
def test_every_cell_runs_every_engine_on_both_architectures(jobs, job_name):
    matrix = jobs[job_name]["strategy"]["matrix"]
    assert sorted(matrix["engine"]) == ENGINES
    assert sorted(matrix["arch"]) == ARCHES, f"{job_name} must cover amd64 and arm64"
    assert jobs[job_name]["strategy"].get("fail-fast") is False


def test_promote_covers_every_engine():
    assert sorted(RELEASE["promote"]["strategy"]["matrix"]["engine"]) == ENGINES


def test_pr_and_release_paths_call_the_same_reusable_definitions():
    """The point of the refactor: one definition of 'the image works'."""
    assert PR["smoke"]["uses"] == RELEASE["candidate-verify"]["uses"] == RELEASE["preflight-smoke"]["uses"]
    assert PR["smoke"]["uses"].endswith("image-smoke.yml")
    assert PR["verify-packages"]["uses"] == RELEASE["verify-packages"]["uses"]
    assert PR["build"]["uses"] == RELEASE["preflight-build"]["uses"] == RELEASE["candidate-build"]["uses"]


def test_the_upload_needs_the_package_gate_and_the_full_image_smoke():
    needs = _needs(RELEASE["publish-packages"])
    assert {"verify-packages", "preflight-smoke"} <= needs


def test_candidates_are_only_built_after_the_upload():
    assert "publish-packages" in _needs(RELEASE["candidate-build"])


def test_a_candidate_is_verified_before_it_can_be_promoted():
    assert "candidate-build" in _needs(RELEASE["candidate-verify"])
    assert "candidate-verify" in _needs(RELEASE["promote"])


def test_every_upload_step_uploads_the_dist_artifact_and_nothing_rebuilds_it():
    steps = RELEASE["publish-packages"]["steps"]
    assert any(
        s.get("uses", "").startswith("actions/download-artifact") and s["with"]["name"] == "dist"
        for s in steps
    )
    publish = [s for s in steps if "pypi-publish" in s.get("uses", "")]
    assert publish and all(s["with"]["packages-dir"] == "dist" for s in publish)
    for name, job in RELEASE.items():
        built = [s for s in job.get("steps", []) if "uv build" in s.get("run", "")]
        assert (name == "prepare") == bool(built), f"only `prepare` may build packages (found in {name})"


def test_the_release_tag_is_written_only_by_promote():
    for name, job in RELEASE.items():
        creates = [s for s in job.get("steps", []) if "imagetools create" in s.get("run", "")]
        assert bool(creates) == (name == "promote"), f"`imagetools create` belongs in promote only, not {name}"


def test_candidates_are_pushed_by_digest_never_by_tag():
    steps = _load("image-build.yml")["jobs"]["build"]["steps"]
    pushing = [s for s in steps if s.get("id") == "push"]
    assert len(pushing) == 1
    with_ = pushing[0]["with"]
    assert "push-by-digest=true" in with_["outputs"] and "push=true" in with_["outputs"]
    assert "tags" not in with_ and "push" not in with_, "a candidate must carry no tag"
    # Every other build step stays local (tarball).
    others = [s for s in steps if "build-push-action" in s.get("uses", "") and s is not pushing[0]]
    assert others and all(s["with"].get("load") is True and "push" not in s["with"] for s in others)


def test_rehearsals_build_images_from_testpypi_and_real_tags_from_pypi():
    text = (WORKFLOWS / "publish-pypi.yml").read_text()
    assert "wheel_source=testpypi" in text and "wheel_source=pypi" in text
    for engine in ENGINES:
        dockerfile = (ROOT / f"Dockerfile.{engine}").read_text()
        # First-party only from TestPyPI: never an extra index for third parties.
        assert "--no-deps --index-url https://test.pypi.org/simple/" in dockerfile
        assert "--extra-index-url" not in dockerfile


def test_tags_trigger_the_release_pipeline_but_not_the_pr_workflow():
    assert "tags" in _triggers("publish-pypi.yml")["push"]
    assert "workflow_dispatch" in _triggers("publish-pypi.yml")
    assert "push" not in _triggers("images.yml"), "images.yml is the PR path; releases are publish-pypi.yml"


def test_the_github_release_waits_for_the_release_pipeline_only():
    text = (WORKFLOWS / "release.yml").read_text()
    assert 'wait_for "publish-pypi.yml"' in text
    assert 'wait_for "images.yml"' not in text, "images.yml no longer runs on tags; waiting on it would hang"
