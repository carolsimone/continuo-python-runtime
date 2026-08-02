"""Shared test fixtures."""

import pytest
import yaml

from continuo_python_runtime.contract.model import Column, Node


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

    def fetch(self, sql):
        return self.tables[sql]

    def ensure_table(self, schema, table, columns):
        self.ensured = (schema, table, columns)

    def load(self, schema, table, data):
        self.loaded = (schema, table, data)

    def close(self):
        pass


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
