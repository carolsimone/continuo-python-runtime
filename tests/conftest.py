"""Shared test fixtures."""

import sys

import pytest
import yaml

from continuo_python_runtime.contract.model import Column, Node


@pytest.fixture
def isolated_import_state():
    """Snapshot/restore ``sys.path`` and ``sys.modules`` around a test.

    ``load_script`` deliberately prepends the repo root and the script's own
    directory to ``sys.path`` and leaves them there for the process lifetime
    (deferred imports inside ``run()`` need them). In a container that is one
    process per node; under pytest it is one process for the whole suite, so
    without this fixture every harness test would leak two ``tmp_path``
    entries into ``sys.path`` and — worse — leave the helper modules it
    imported (``helpers``, ``scripts.helpers``, …) cached in ``sys.modules``,
    where a later test importing the same name would silently get the earlier
    test's file. Any test whose script imports an in-repo helper must use
    this fixture.
    """
    original_path = list(sys.path)
    original_modules = set(sys.modules)
    try:
        yield
    finally:
        sys.path[:] = original_path
        for name in set(sys.modules) - original_modules:
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


class FakeRuntimeAdapter:
    """In-memory stand-in for a RuntimeAdapter, for harness tests."""

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

    def ensure_table(self, schema, table, columns, *, config=None):
        # config is keyword-ONLY on purpose: docs/boundary-contract.md §13.4
        # makes keyword-passing normative for every third-party
        # RuntimeAdapter, and a signature that also accepts it positionally
        # would let the harness regress to a positional call unnoticed.
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
