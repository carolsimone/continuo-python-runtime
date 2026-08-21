"""Shared fixtures for adapters/postgres/tests.

``adapters/postgres/tests`` is its own top-level pytest package (it carries
``__init__.py``, per the root pyproject's import-mode note), so it cannot see
fixtures declared in the root ``tests/conftest.py`` — pytest only walks
conftest files up a test's own directory ancestry, and this directory is not
an ancestor of the root ``tests/`` package. ``minio_container`` is duplicated
here rather than imported, matching the root fixture byte-for-byte in
behavior (real minio via a plain ``docker run``, dynamic host port, health
wait, ``docker rm -f`` teardown) so the postgres-adapter integration suite
gets the same "real backends, no stubs, no silent skip" guarantee without a
cross-package import.
"""

import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import pytest


@pytest.fixture(scope="session")
def minio_container():
    """Session-scoped real minio backend, started via a plain `docker run`.

    No testcontainers/pytest-docker dependency in this repo, so this drives
    docker directly. Publishes minio's 9000 to an EPHEMERAL host port (colima
    may already hold 9000 for another stack) and waits for minio's own
    /minio/health/live endpoint to return 200 before yielding. Any failure —
    docker missing, `docker run` erroring, the health check never turning
    green — raises so the dependent tests error loudly instead of silently
    skipping, per this suite's "real backends, no stubs, no silent skip"
    integration-testing policy.
    """
    name = f"postgres-adapter-minio-{uuid.uuid4().hex[:12]}"
    try:
        subprocess.run(
            [
                "docker", "run", "-d", "--name", name,
                "-p", "0:9000",
                "-e", "MINIO_ROOT_USER=minioadmin",
                "-e", "MINIO_ROOT_PASSWORD=minioadmin",
                "minio/minio:latest",
                "server", "/data", "--address", ":9000",
            ],
            check=True, capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "docker is not available; the postgres adapter's csv integration "
            "test requires a live docker daemon"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"docker run for minio failed: {exc.stderr}") from exc

    try:
        port_out = subprocess.run(
            ["docker", "port", name, "9000/tcp"],
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        # e.g. "0.0.0.0:54321\n[::]:54321" -- take the first (ipv4) mapping.
        host_port = port_out.splitlines()[0].rsplit(":", 1)[1]
        endpoint = f"http://127.0.0.1:{host_port}"

        deadline = time.monotonic() + 30
        last_error: Exception | None = None
        healthy = False
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"{endpoint}/minio/health/live", timeout=2
                ) as resp:
                    if resp.status == 200:
                        healthy = True
                        break
            except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
                last_error = exc
            time.sleep(0.5)
        if not healthy:
            raise RuntimeError(
                f"minio container {name} never became healthy at "
                f"{endpoint}/minio/health/live: {last_error}"
            )

        # make_s3_client() forwards only S3_ENDPOINT_URL and otherwise leaves
        # credentials to boto3's own chain, so the chain needs something to
        # find. This is session-scoped (not monkeypatch, which is
        # function-scoped) -- set directly and restore on teardown.
        # WARNING: any other test that runs while this fixture is active and
        # makes a REAL (non-mocked, non-S3_ENDPOINT_URL-redirected) AWS call
        # would authenticate with these fake minioadmin credentials, not the
        # caller's real ones.
        prior_key = os.environ.get("AWS_ACCESS_KEY_ID")
        prior_secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
        os.environ["AWS_ACCESS_KEY_ID"] = "minioadmin"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "minioadmin"
        try:
            yield (endpoint, "minioadmin", "minioadmin")
        finally:
            if prior_key is None:
                os.environ.pop("AWS_ACCESS_KEY_ID", None)
            else:
                os.environ["AWS_ACCESS_KEY_ID"] = prior_key
            if prior_secret is None:
                os.environ.pop("AWS_SECRET_ACCESS_KEY", None)
            else:
                os.environ["AWS_SECRET_ACCESS_KEY"] = prior_secret
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, text=True, timeout=30
        )
