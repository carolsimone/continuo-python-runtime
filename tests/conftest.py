"""Shared test fixtures."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from continuo_engine_contract.port import AppendCapable, LiveColumn, UpsertCapable

from continuo_python_runtime.contract.loader import load_contract_dir
from continuo_python_runtime.contract.model import Node
from tests.odcs_builder import contract_node, write_contract

DEMO = Path(__file__).resolve().parent / "fixtures" / "demo" / "service-py"
DEMO_CONTRACTS = DEMO / "contracts"
DEMO_OLD = DEMO / "old"

# The scripts the demo contracts name, with fixed bytes so that every hash made from them is a
# constant. py_daily_kpis imports kpi_math, so its import closure is not empty.
DEMO_SCRIPTS = {
    "scripts/py_daily_kpis.py": b"import kpi_math\n\n\ndef run(ctx):\n    return kpi_math.total(ctx.read('daily'))\n",
    "scripts/kpi_math.py": b"def total(rows):\n    return rows\n",
    "scripts/fx_eur_usd_rates.py": b"def run(ctx):\n    return None\n",
}

# The one node of `contract_repo` and `harness_repo`, and of `node_fixture`.
NODE_T = {
    "schema": "analytics", "table": "t", "owner": "m", "schedule": "daily",
    "criticality": "SECONDARY", "script": "scripts/t.py",
    "reads": {"ids": "select id from analytics.a"},
    "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
}


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


def _node_repo(root: Path) -> Path:
    """Write a repository of one node, ``analytics.t``: its script and ``contracts/t.odcs.yaml``."""
    (root / "scripts").mkdir()
    (root / "scripts" / "t.py").write_text("def run(ctx):\n    return ctx.read('ids')\n")
    write_contract(root / "contracts", **NODE_T)
    return root


@pytest.fixture
def contract_repo(tmp_path):
    """A minimal contract repository: one node's script and its ODCS contract."""
    return _node_repo(tmp_path)


@pytest.fixture
def demo_contracts(tmp_path):
    """A copy of the demo's three ODCS contracts, in ``<tmp>/contracts``."""
    return Path(shutil.copytree(DEMO_CONTRACTS, tmp_path / "contracts"))


@dataclass(frozen=True)
class DemoRepo:
    """The demo's repository: ``contracts`` holds its three ODCS contracts, ``old`` the
    ``nodes:`` files of the format they replaced, ``root`` the scripts they name and
    ``packages_file`` the package list of the image it runs, as ``pack`` reads it."""

    root: Path
    contracts: Path
    old: Path
    packages_file: Path

    def old_entry(self, table: str) -> dict[str, Any]:
        """The one ``nodes:`` entry of ``old/<table>.yml``."""
        (entry,) = yaml.safe_load((self.old / f"{table}.yml").read_text(encoding="utf-8"))["nodes"]
        return entry

    def node(self, table: str) -> Node:
        """The node ``contracts/<table>.odcs.yaml`` declares, as the loader reads it."""
        (node,) = (n for n in load_contract_dir(self.contracts) if n.table == table)
        return node


@pytest.fixture
def demo_repo(tmp_path, demo_contracts):
    """The demo's contracts in ``<tmp>/contracts``, the old files in ``<tmp>/old`` and its scripts."""
    for relative, data in DEMO_SCRIPTS.items():
        script = tmp_path / relative
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_bytes(data)
    old = Path(shutil.copytree(DEMO_OLD, tmp_path / "old"))
    packages_file = tmp_path / "image-packages.json"
    packages_file.write_text(json.dumps({"pyarrow": "26.0.0"}), encoding="utf-8")
    return DemoRepo(root=tmp_path, contracts=demo_contracts, old=old, packages_file=packages_file)


@dataclass(frozen=True)
class FakeDocker:
    """The ``docker`` executable `fake_docker` put first on ``PATH``: ``calls`` reads the
    arguments of its last call, one per line."""

    executable: Path

    @property
    def calls(self) -> list[str]:
        return (self.executable.parent / "calls").read_text(encoding="utf-8").splitlines()


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    """Return a function that installs a ``docker`` which records its arguments, prints
    ``stdout`` and ``stderr`` and exits with ``exit_code``, first on ``PATH``."""

    def install(*, stdout: str = "", stderr: str = "", exit_code: int = 0) -> FakeDocker:
        folder = tmp_path / "fake-docker"
        folder.mkdir(exist_ok=True)
        executable = folder / "docker"
        executable.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$@\" > {shlex.quote(str(folder / 'calls'))}\n"
            f"printf '%s' {shlex.quote(stdout)}\n"
            f"printf '%s' {shlex.quote(stderr)} >&2\n"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
        return FakeDocker(executable)

    return install


@pytest.fixture
def no_docker(tmp_path, monkeypatch):
    """Leave no ``docker`` on ``PATH``: it holds one empty folder."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


@pytest.fixture
def node_fixture():
    """A Node with a single declared read."""
    return contract_node(**NODE_T)


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


class FakeAppendAdapter(FakeWarehouseAdapter, AppendCapable):
    """FakeWarehouseAdapter that declares AppendCapable.

    ``live_columns`` is what table_columns reports; None reports exactly the
    columns ensure_table received (a table the harness just created).
    """

    def __init__(self, tables=None, live_columns=None):
        super().__init__(tables)
        self.live_columns = live_columns
        self.introspected = None
        self.appended = None

    def table_columns(self, schema, table):
        self.introspected = (schema, table)
        if self.live_columns is not None:
            return list(self.live_columns)
        return [LiveColumn(c["name"], c["nullable"]) for c in self.ensured[2]]

    def append(self, schema, table, data):
        self.appended = (schema, table, data)


class FakeUpsertAdapter(FakeWarehouseAdapter, UpsertCapable):
    """FakeWarehouseAdapter that declares UpsertCapable; upsert() is the port's own template method."""

    def __init__(self, tables=None, live_columns=None):
        super().__init__(tables)
        self.live_columns = live_columns
        self.introspected = None
        self.ensured_primary_key = None
        self.upserted = None

    def ensure_table(self, schema, table, columns, *, config, primary_key=()):
        super().ensure_table(schema, table, columns, config=config)
        self.ensured_primary_key = tuple(primary_key)

    def table_columns(self, schema, table):
        self.introspected = (schema, table)
        if self.live_columns is not None:
            return list(self.live_columns)
        return [LiveColumn(c["name"], c["nullable"]) for c in self.ensured[2]]

    def _upsert(self, schema, table, data, primary_key):
        self.upserted = (schema, table, data, tuple(primary_key))


@pytest.fixture
def harness_repo(tmp_path):
    """A minimal repository for the harness: one node's script and its ODCS contract."""
    return _node_repo(tmp_path)


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
                # ghcr mirror of Bitnami's minio image (Docker Hub's minio/minio
                # is no longer anonymously pullable). Its ENTRYPOINT is
                # Bitnami's own setup script, not the `minio` binary, so it
                # must be overridden for these args to reach `minio` directly;
                # its data dir also has to be Bitnami's own
                # /bitnami/minio/data (world-root-group-writable for the
                # image's non-root user) since plain /data doesn't exist in
                # this image and the binary can't create it at the
                # filesystem root.
                "--entrypoint", "minio",
                "ghcr.io/carolsimone/continuo-minio:2025.7.23",
                "server", "/bitnami/minio/data", "--address", ":9000",
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
