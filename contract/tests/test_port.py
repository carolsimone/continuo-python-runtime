"""Discovery tests for the WarehouseAdapter port.

These install REAL adapter packages (with genuine ``continuo_engine.adapters``
entry points) into throwaway virtualenvs and run ``discover_adapter`` against the
real ``importlib.metadata`` machinery — no monkeypatching of ``entry_points``, so the
tests exercise actual entry-point loading rather than an assumed return shape.
"""
import subprocess
import textwrap

from pathlib import Path

import pytest

from continuo_engine_contract.port import ENTRY_POINT_GROUP, WarehouseAdapter

CONTRACT_ROOT = Path(__file__).resolve().parent.parent  # contract/


def test_warehouse_adapter_cannot_be_instantiated():
    """The port is abstract — a concrete adapter must implement every method."""
    with pytest.raises(TypeError):
        WarehouseAdapter()  # type: ignore[abstract]


def test_warehouse_adapter_requires_drop_schema():
    """drop_schema is part of the contract: omitting it blocks instantiation.

    A concrete adapter that implements every method except the teardown one must
    still be abstract, so the executor can rely on drop_schema existing on every
    installed engine.
    """

    class MissingDrop(WarehouseAdapter):
        @classmethod
        def required_env(cls):
            return []

        @classmethod
        def from_env(cls):
            return cls()

        def ensure_schema(self, schema):
            ...

        def build_empty_from_sql(self, schema, table, compiled_sql):
            ...

        def clone_empty_from_prod(self, candidate_schema, prod_schema, table):
            ...

        def build_empty_from_columns(self, schema, table, columns, config):
            ...

        def check_binds(self, sql):
            ...

        def fetch(self, sql):
            ...

        def ensure_table(self, schema, table, columns, *, config):
            ...

        def load(self, schema, table, data):
            ...

        def close(self):
            ...

    with pytest.raises(TypeError, match="drop_schema"):
        MissingDrop()  # type: ignore[abstract]


def test_warehouse_adapter_requires_columns_and_binds_methods():
    """A subclass missing the 0.4.0 methods cannot be instantiated.

    Adapters implementing only the 0.3.0 surface (required_env, from_env,
    ensure_schema, drop_schema, build_empty_from_sql, clone_empty_from_prod,
    close) must still be abstract, so callers can rely on build_empty_from_columns
    and check_binds existing on every installed engine.
    """

    class Partial(WarehouseAdapter):
        @classmethod
        def required_env(cls):
            return []

        @classmethod
        def from_env(cls):
            return cls()

        def ensure_schema(self, schema):
            ...

        def drop_schema(self, schema):
            ...

        def build_empty_from_sql(self, schema, table, compiled_sql):
            ...

        def clone_empty_from_prod(self, candidate_schema, prod_schema, table):
            ...

        def close(self):
            ...

    with pytest.raises(TypeError, match="build_empty_from_columns|check_binds"):
        Partial()  # type: ignore[abstract]


def test_partial_engine_cannot_be_instantiated():
    """A class implementing only the validation half must fail at construction.

    One port means one class per engine covering both roles: an adapter that
    still only speaks validation DDL is abstract, so a half-migrated engine
    package cannot be constructed and silently fail later, at the first
    python-node read.
    """

    class HalfEngine(WarehouseAdapter):
        @classmethod
        def required_env(cls):
            return []

        @classmethod
        def from_env(cls):
            return cls()

        def ensure_schema(self, schema):
            ...

        def drop_schema(self, schema):
            ...

        def build_empty_from_sql(self, schema, table, compiled_sql):
            ...

        def build_empty_from_columns(self, schema, table, columns, config):
            ...

        def clone_empty_from_prod(self, schema, prod_schema, table):
            ...

        def check_binds(self, sql):
            ...

        def close(self):
            ...

        # fetch / ensure_table / load deliberately missing

    with pytest.raises(TypeError):
        HalfEngine()  # type: ignore[abstract]


def test_single_entry_point_group_and_single_discovery():
    """One port, one entry-point group, one discovery function — no second half left."""
    from continuo_engine_contract import port

    assert port.ENTRY_POINT_GROUP == "continuo_engine.adapters"
    assert not hasattr(port, "RUNTIME_ENTRY_POINT_GROUP")
    assert not hasattr(port, "discover_runtime_adapter")
    assert not hasattr(port, "ValidationAdapter")
    assert not hasattr(port, "RuntimeAdapter")


def test_ensure_table_signature_declares_config_keyword_only():
    """The port itself declares the physical-layout config parameter.

    Adapters receive the node's config through the abstract signature — they
    must never have to extend beyond the ABC to accept it. It is required and
    keyword-only: the harness passes ``config=`` unconditionally, and a
    positional-friendly signature would let callers regress to positional
    passing unnoticed.
    """
    import inspect

    sig = inspect.signature(WarehouseAdapter.ensure_table)
    params = list(sig.parameters)
    assert params == ["self", "schema", "table", "columns", "config"]
    config = sig.parameters["config"]
    assert config.kind is inspect.Parameter.KEYWORD_ONLY
    assert config.default is inspect.Parameter.empty


# --- Real installed-adapter discovery via throwaway venvs --------------------

_FIXTURE_MODULE = '''\
from continuo_engine_contract.port import WarehouseAdapter


class OneAdapter(WarehouseAdapter):
    @classmethod
    def required_env(cls):
        return []

    @classmethod
    def from_env(cls):
        return cls()

    def ensure_schema(self, schema):
        ...

    def drop_schema(self, schema):
        ...

    def build_empty_from_sql(self, schema, table, compiled_sql):
        ...

    def clone_empty_from_prod(self, candidate_schema, prod_schema, table):
        ...

    def build_empty_from_columns(self, schema, table, columns, config):
        ...

    def check_binds(self, sql):
        ...

    def fetch(self, sql):
        ...

    def ensure_table(self, schema, table, columns, *, config):
        ...

    def load(self, schema, table, data):
        ...

    def close(self):
        ...


class TwoAdapter(OneAdapter):
    ...


not_an_adapter = object()
'''

_DISCOVER_SCRIPT = textwrap.dedent(
    """
    from continuo_engine_contract.port import discover_adapter, AdapterDiscoveryError
    try:
        name, cls = discover_adapter()
        print("OK", name, cls.__name__)
    except AdapterDiscoveryError as exc:
        print("ERR", exc)
    """
)


def _run_discovery(
    tmp_path: Path,
    entry_points: dict,
    module_body: str = _FIXTURE_MODULE,
    group: str = ENTRY_POINT_GROUP,
    script: str = _DISCOVER_SCRIPT,
) -> str:
    """Run discovery in a fresh venv holding the contract + a fixture adapter.

    Installs a generated fixture package declaring *entry_points* under *group*
    with *module_body* as its module source, then runs *script* (which calls
    ``discover_adapter``) and returns its stdout.
    """
    fixture = tmp_path / "fixture"
    (fixture / "cvfixtureadapters").mkdir(parents=True)
    (fixture / "cvfixtureadapters" / "__init__.py").write_text(module_body)
    eps = "\n".join(f'{name} = "cvfixtureadapters:{target}"' for name, target in entry_points.items())
    (fixture / "pyproject.toml").write_text(
        textwrap.dedent(
            f"""
            [project]
            name = "cvfixtureadapters"
            version = "0.0.0"
            requires-python = ">= 3.14"

            [project.entry-points."{group}"]
            {eps}

            [build-system]
            requires = ["hatchling"]
            build-backend = "hatchling.build"

            [tool.hatch.build.targets.wheel]
            packages = ["cvfixtureadapters"]
            """
        )
    )
    venv = tmp_path / "venv"
    subprocess.run(["uv", "venv", str(venv), "-p", "3.14"], check=True, capture_output=True)
    subprocess.run(
        ["uv", "pip", "install", "--python", str(venv), str(CONTRACT_ROOT), str(fixture)],
        check=True,
        capture_output=True,
    )
    py = venv / "bin" / "python"
    proc = subprocess.run([str(py), "-c", script], capture_output=True, text=True)
    return proc.stdout


def test_discover_adapter_raises_when_none_installed(tmp_path):
    """With no adapter package installed, discovery raises cleanly.

    This runs in a throwaway venv rather than the ambient one: the ambient
    environment in this workspace also has other, unrelated adapter packages
    installed alongside the contract, so it can never exercise the genuine
    zero-adapter path.
    """
    out = _run_discovery(tmp_path, {})
    assert out.startswith("ERR")
    assert "no engine adapter installed" in out


def test_discover_single_installed_adapter_is_found(tmp_path):
    """One installed adapter package → discovery returns its name and class."""
    out = _run_discovery(tmp_path, {"one": "OneAdapter"})
    assert out.strip() == "OK one OneAdapter"


def test_discover_multiple_installed_adapters_raises_naming_them(tmp_path):
    """Two installed adapters → discovery refuses and names both."""
    out = _run_discovery(tmp_path, {"one": "OneAdapter", "two": "TwoAdapter"})
    assert out.startswith("ERR")
    assert "one, two" in out


def test_discover_non_adapter_entry_point_raises(tmp_path):
    """An entry point that is not a WarehouseAdapter subclass is rejected."""
    out = _run_discovery(tmp_path, {"broken": "not_an_adapter"})
    assert out.startswith("ERR")
    assert "WarehouseAdapter subclass" in out


def test_discover_wraps_adapter_import_failure(tmp_path):
    """A failing adapter import surfaces as AdapterDiscoveryError, not a raw ImportError.

    That lets the runner still emit a structured result block for a malformed engine
    image rather than crashing with a traceback.
    """
    out = _run_discovery(
        tmp_path,
        {"one": "OneAdapter"},
        module_body="import definitely_missing_pkg_zzz  # noqa: F401\n",
    )
    assert out.startswith("ERR")
    assert "failed to load adapter entry point" in out
