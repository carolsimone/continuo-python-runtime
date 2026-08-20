r"""Tier-4 image smoke tests: a built dual-role engine image fails structurally.

Needs the image built first, e.g.:
    docker build -f Dockerfile.postgres -t continuo-python-runtime-postgres:dev .
Select the tag under test with VALIDATION_IMAGE_UNDER_TEST, and the engine it bakes
with VALIDATION_IMAGE_ENGINE / VALIDATION_IMAGE_REQUIRED_ENV (the adapter's first
required connection var), so the same tests cover every engine image.

The image's ENTRYPOINT is ["continuo-runtime"] with CMD ["run"]: a bare
`docker run IMAGE` dispatches the node harness, so every invocation meant to
exercise the validation runner must append "validation-op".
"""
import os
import subprocess

import pytest

from continuo_engine_contract import result

IMAGE = os.environ.get("VALIDATION_IMAGE_UNDER_TEST", "continuo-python-runtime-postgres:dev")
ENGINE = os.environ.get("VALIDATION_IMAGE_ENGINE", "postgres")
REQUIRED_ENV = os.environ.get("VALIDATION_IMAGE_REQUIRED_ENV", "POSTGRES_HOST")


def _run(env: dict) -> subprocess.CompletedProcess:
    args = ["docker", "run", "--rm"]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    return subprocess.run(
        args + [IMAGE, "validation-op"], capture_output=True, text=True, timeout=120
    )


@pytest.mark.image
def test_no_env_exits_2_with_structured_block():
    """No env at all: the runner exits 2, naming the first missing var on stderr."""
    proc = _run({})
    assert proc.returncode == 2
    assert "missing required env var DBT_TARGET_SCHEMA" in proc.stderr


@pytest.mark.image
def test_discovery_and_required_env_produce_structured_block():
    """The baked adapter is discovered; its missing connection env fails cleanly."""
    proc = _run({
        "DBT_TARGET_SCHEMA": "_candidate_smoke",
        "TABLE_NAME": "t",
        "VALIDATION_OP": "clone_from_prod",
        "PROD_SCHEMA": "analytics",
    })
    # Discovery loaded exactly one adapter; its required env is absent, so the
    # runner emits a structured block naming the missing vars and exits 2.
    assert proc.returncode == 2
    assert result.SENTINEL_BEGIN in proc.stdout
    assert REQUIRED_ENV in proc.stdout
    assert ENGINE in proc.stdout


@pytest.mark.image
def test_default_command_is_the_node_harness():
    """docker run IMAGE with no args must dispatch `continuo-runtime run` —
    the domain-repo contract. It fails fast on missing NODE_ID, proving the
    harness (not the validation runner) answered."""
    proc = subprocess.run(
        ["docker", "run", "--rm", os.environ["VALIDATION_IMAGE_UNDER_TEST"]],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "NODE_ID" in proc.stdout + proc.stderr
