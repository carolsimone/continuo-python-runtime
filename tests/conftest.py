"""Shared test fixtures."""

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest
import yaml

from continuo_python_runtime.contract.model import Column, Node


def _module_roots(module):
    """Every filesystem location *module* was loaded from (`__path__` for packages)."""
    origin = getattr(module, "__file__", None)
    if origin:
        return [origin]
    # Namespace packages have __file__ is None but a non-empty __path__.
    return list(getattr(module, "__path__", []) or [])


@pytest.fixture(autouse=True)
def isolated_import_state():
    """Undo, after every test, whatever the test added to the import system.

    ``load_script`` deliberately prepends the repo root and the script's own
    directory to ``sys.path`` and leaves them there for the process lifetime,
    because a deferred import inside ``run()`` needs them and a container runs
    exactly one node per process. Under pytest that same process runs the
    whole suite, so the side effect has to be undone here or it accumulates:
    two ``tmp_path`` entries per harness test, and — the sharp edge — a helper
    module cached in ``sys.modules`` under a plain name like ``helpers``,
    which a later test importing the same name would silently get instead of
    its own file.

    Autouse rather than opt-in: a future test whose script imports a helper
    would otherwise contaminate the suite by omission, and the observable
    symptom (a stale helper) looks nothing like the cause. Eviction is
    deliberately narrow — only modules newly added to ``sys.modules`` *and*
    loaded from one of the ``sys.path`` entries this test introduced — so a
    lazily-imported third-party submodule (``pyarrow.compute``, …) is never
    evicted and re-imported behind a caller still holding the old object.
    """
    original_path = list(sys.path)
    original_modules = set(sys.modules)
    try:
        yield
    finally:
        added = [entry for entry in sys.path if entry not in original_path]
        sys.path[:] = original_path
        if not added:
            return
        roots = tuple(str(Path(entry).resolve()) + os.sep for entry in added)
        for name in set(sys.modules) - original_modules:
            module = sys.modules.get(name)
            if module is None:
                continue
            if any(
                str(Path(location).resolve()).startswith(roots)
                for location in _module_roots(module)
            ):
                del sys.modules[name]


@pytest.fixture
def contract_repo(tmp_path):
    """Create a minimal contract repository with scripts and YAML files."""
    (tmp_path / "contracts").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "t.py").write_text("def run(ctx):\n    return ctx.read('ids')\n")
    (tmp_path / "contracts" / "t.yml").write_text(yaml.safe_dump({"nodes": [{
        "schema": "analytics", "table": "t", "owner": "m", "schedule": "daily",
        "criticality": "SECONDARY", "script": "scripts/t.py",
        "reads": {"ids": "select id from analytics.a"},
        "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
    }]}))
    return tmp_path


@pytest.fixture
def node_fixture():
    """A Node with a single declared read."""
    return Node(
        schema="analytics",
        table="t",
        owner="m",
        schedule="daily",
        criticality="SECONDARY",
        script="scripts/t.py",
        reads={"ids": "select id from analytics.a"},
        output_columns=(Column("id", "INTEGER", nullable=False),),
    )


class FakeWarehouseAdapter:
    """In-memory stand-in for a WarehouseAdapter, for harness tests."""

    def __init__(self, tables=None):
        self.tables = tables or {}  # sql -> pa.Table
        self.loaded = None
        self.ensured = None
        self.ensured_config = None
        self.closed = False

    @classmethod
    def required_env(cls):
        return []

    @classmethod
    def from_env(cls):
        return cls()

    def fetch(self, sql):
        return self.tables[sql]

    def ensure_table(self, schema, table, columns, *, config):
        # config is keyword-ONLY and required, matching the contract 0.6.0
        # port: docs/boundary-contract.md §13.4 makes keyword-passing
        # normative, and a signature that also accepts it positionally would
        # let the harness regress to a positional call unnoticed.
        self.ensured = (schema, table, columns)
        self.ensured_config = config

    def load(self, schema, table, data):
        self.loaded = (schema, table, data)

    def close(self):
        self.closed = True


@pytest.fixture
def harness_repo(tmp_path):
    """Create a minimal contract repository with scripts and YAML files."""
    (tmp_path / "contracts").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "t.py").write_text("def run(ctx):\n    return ctx.read('ids')\n")
    (tmp_path / "contracts" / "t.yml").write_text(yaml.safe_dump({"nodes": [{
        "schema": "analytics", "table": "t", "owner": "m", "schedule": "daily",
        "criticality": "SECONDARY", "script": "scripts/t.py",
        "reads": {"ids": "select id from analytics.a"},
        "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
    }]}))
    return tmp_path


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
    name = f"csv-readers-minio-{uuid.uuid4().hex[:12]}"
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
            "docker is not available; the csv reader integration tests "
            "require a live docker daemon"
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
