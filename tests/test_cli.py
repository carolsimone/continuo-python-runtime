"""Tests for CLI subcommands: validate, pack, hash, packages, lint, run and validation-op."""

import hashlib
import json
from importlib import metadata

import pyarrow as pa
import pytest

import continuo_python_runtime.cli as cli_module
from continuo_python_runtime.cli import main
from continuo_python_runtime.hash_inputs import gather_hash_inputs
from continuo_python_runtime.hashing import hash_parts, shared_code_hash
from tests.conftest import DEMO_SCRIPTS, NODE_T, FakeWarehouseAdapter
from tests.odcs_builder import write_contract

__version__ = metadata.version("continuo-python-runtime")


def test_version_prints_the_installed_runtime_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out == f"continuo-runtime {metadata.version('continuo-python-runtime')}\n"


def test_validate_ok(contract_repo):
    assert main(["validate", str(contract_repo / "contracts")]) == 0


def test_validate_bad_dir_exits_1(tmp_path, caplog):
    assert main(["validate", str(tmp_path)]) == 1
    assert any("no contract files" in r.message for r in caplog.records)


def test_validate_dialect_flag_threads_to_loader(contract_repo, monkeypatch):
    captured = {}

    def fake_load_contract_dir(path, *, dialect=None):
        captured["dialect"] = dialect
        return []

    monkeypatch.setattr(cli_module, "load_contract_dir", fake_load_contract_dir)
    rc = main(["validate", str(contract_repo / "contracts"), "--dialect", "postgres"])
    assert rc == 0
    assert captured["dialect"] == "postgres"


def test_validate_dialect_defaults_to_none(contract_repo, monkeypatch):
    captured = {}

    def fake_load_contract_dir(path, *, dialect=None):
        captured["dialect"] = dialect
        return []

    monkeypatch.setattr(cli_module, "load_contract_dir", fake_load_contract_dir)
    rc = main(["validate", str(contract_repo / "contracts")])
    assert rc == 0
    assert captured["dialect"] is None


def test_validate_invalid_dialect_reports_bad_flag_not_bad_read(contract_repo, caplog):
    """End-to-end (no mocking of load_contract_dir): a typo'd --dialect value
    against a contract whose only read is perfectly valid SQL must exit 1
    with a message naming --dialect, not one that reads as though the SQL
    itself were rejected."""
    rc = main(["validate", str(contract_repo / "contracts"), "--dialect", "POSTGRES"])
    assert rc == 1
    messages = [r.message for r in caplog.records]
    assert any("--dialect" in m and "POSTGRES" in m for m in messages)
    assert not any("must be a single read query" in m for m in messages)


def test_validate_refuses_a_read_that_names_a_table_without_its_schema(tmp_path, caplog):
    write_contract(
        tmp_path,
        schema="analytics",
        table="t",
        owner="marketing",
        schedule="daily",
        criticality="SECONDARY",
        script="scripts/t.py",
        reads={"ids": "select id from orders"},
        output_columns=[{"name": "id", "type": "INTEGER"}],
    )
    assert main(["validate", str(tmp_path)]) == 1
    messages = [r.message for r in caplog.records]
    assert any(
        "read 'ids' names table 'orders' without its schema; write <schema>.orders" in m
        for m in messages
    )


def test_validate_refuses_a_read_that_starts_with_a_semicolon_without_a_traceback(tmp_path, caplog):
    write_contract(
        tmp_path,
        schema="analytics",
        table="t",
        owner="marketing",
        schedule="daily",
        criticality="SECONDARY",
        script="scripts/t.py",
        reads={"ids": "; select id from raw.a"},
        output_columns=[{"name": "id", "type": "INTEGER"}],
    )
    assert main(["validate", str(tmp_path)]) == 1
    assert any("'reads.ids' must be a single read query" in r.message for r in caplog.records)


def test_pack_writes_the_bundle(demo_repo, tmp_path):
    out = tmp_path / "odcs-bundle.json"
    assert main(["pack", str(demo_repo.contracts), "--service", "service-py", "--repo-root", str(demo_repo.root),
                 "--image-packages", str(demo_repo.packages_file), "--out", str(out)]) == 0
    doc = json.loads(out.read_text())
    assert doc["bundle_version"] == 1 and doc["service"] == "service-py"
    assert doc["producer"] == {"continuo_python_runtime": __version__, "continuo_contracts": "0.4.1"}
    assert doc["image_packages"] == {"pyarrow": "26.0.0"}
    assert [n["path"] for n in doc["nodes"]] == [
        "contracts/demo_orders_csv.odcs.yaml", "contracts/fx_eur_usd_rates.odcs.yaml", "contracts/py_daily_kpis.odcs.yaml"
    ]
    kpis = doc["nodes"][2]
    assert kpis["text"] == (demo_repo.contracts / "py_daily_kpis.odcs.yaml").read_text()
    assert kpis["shared_code_hash"] == shared_code_hash({"scripts/kpi_math.py": DEMO_SCRIPTS["scripts/kpi_math.py"]})
    assert doc["nodes"][0]["source_hash"] == hashlib.sha256(b"s3://continuo-dev/static-files/demo/orders.csv").hexdigest()
    assert doc["nodes"][0]["shared_code_hash"] == ""
    assert out.read_bytes() == (json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def test_pack_records_the_packages_of_the_image_not_of_this_environment(demo_repo, tmp_path, fake_docker):
    docker = fake_docker(stdout='{"pyarrow": "26.0.0", "somelib": "1.2.3"}\n')
    out = tmp_path / "odcs-bundle.json"
    assert main(["pack", str(demo_repo.contracts), "--service", "service-py", "--repo-root", str(demo_repo.root),
                 "--image", "registry/service-py:7", "--out", str(out)]) == 0
    assert json.loads(out.read_text())["image_packages"] == {"pyarrow": "26.0.0", "somelib": "1.2.3"}
    assert docker.calls == ["run", "--rm", "--entrypoint", "continuo-runtime", "registry/service-py:7", "packages"]


def test_pack_with_no_docker_writes_nothing(demo_repo, tmp_path, no_docker, caplog):
    out = tmp_path / "odcs-bundle.json"
    rc = main(["pack", str(demo_repo.contracts), "--service", "service-py", "--repo-root", str(demo_repo.root),
               "--image", "registry/service-py:7", "--out", str(out)])
    assert rc == 1
    assert not out.exists()
    assert any("registry/service-py:7" in r.message and "docker" in r.message for r in caplog.records)


def test_pack_refuses_an_image_without_the_packages_command(demo_repo, tmp_path, fake_docker, caplog):
    fake_docker(stderr="continuo-runtime: error: argument command: invalid choice: 'packages'\n", exit_code=2)
    out = tmp_path / "odcs-bundle.json"
    rc = main(["pack", str(demo_repo.contracts), "--service", "service-py", "--repo-root", str(demo_repo.root),
               "--image", "registry/service-py:7", "--out", str(out)])
    assert rc == 1
    assert not out.exists()
    assert any(
        "registry/service-py:7" in r.message
        and "the image has no `packages` command: build it on continuo-python-runtime 0.13.0 or later" in r.message
        for r in caplog.records
    )


@pytest.mark.parametrize("flags", ["neither", "both"])
def test_pack_needs_exactly_one_package_source(demo_repo, tmp_path, capsys, flags):
    out = tmp_path / "odcs-bundle.json"
    argv = ["pack", str(demo_repo.contracts), "--service", "service-py", "--repo-root", str(demo_repo.root),
            "--out", str(out)]
    if flags == "both":
        argv += ["--image", "registry/service-py:7", "--image-packages", str(demo_repo.packages_file)]
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert "records the package list of the image" in capsys.readouterr().err
    assert not out.exists()


def test_pack_refuses_a_contract_of_another_service(demo_repo, tmp_path, caplog):
    write_contract(demo_repo.contracts, **{**NODE_T, "table": "other_service", "script": "scripts/t.py", "service": "other"})
    out = tmp_path / "odcs-bundle.json"
    rc = main(["pack", str(demo_repo.contracts), "--service", "service-py", "--repo-root", str(demo_repo.root),
               "--image-packages", str(demo_repo.packages_file), "--out", str(out)])
    assert rc == 1
    assert not out.exists()
    assert any("contracts/other_service.odcs.yaml" in r.message and "'other'" in r.message for r in caplog.records)


def test_pack_refuses_two_contracts_for_one_table_in_another_case(demo_repo, tmp_path, caplog):
    plain = demo_repo.contracts / "py_daily_kpis.odcs.yaml"
    mixed = demo_repo.contracts / "analytics.Py_Daily_Kpis.odcs.yaml"
    mixed.write_text(
        plain.read_text(encoding="utf-8").replace("name: py_daily_kpis", "name: Py_Daily_Kpis"),
        encoding="utf-8",
    )
    out = tmp_path / "odcs-bundle.json"
    rc = main(["pack", str(demo_repo.contracts), "--service", "service-py", "--repo-root", str(demo_repo.root),
               "--image-packages", str(demo_repo.packages_file), "--out", str(out)])
    assert rc == 1
    assert not out.exists()
    assert any(
        f"duplicate node analytics.py_daily_kpis (in {mixed} and {plain})" in r.message
        for r in caplog.records
    )


def test_pack_lints_each_closure(demo_repo, tmp_path, caplog):
    (demo_repo.root / "scripts" / "kpi_math.py").write_text("import psycopg2\n")
    out = tmp_path / "odcs-bundle.json"
    rc = main(["pack", str(demo_repo.contracts), "--service", "service-py", "--repo-root", str(demo_repo.root),
               "--image-packages", str(demo_repo.packages_file), "--out", str(out)])
    assert rc == 1
    assert not out.exists()
    assert any(
        "analytics.py_daily_kpis" in r.message and "scripts/kpi_math.py:1: forbidden warehouse driver import 'psycopg2'" in r.message
        for r in caplog.records
    )


def test_pack_refuses_a_startup_hook_at_the_repository_root(demo_repo, tmp_path, caplog):
    (demo_repo.root / "sitecustomize.py").write_text("X = 1\n")
    out = tmp_path / "odcs-bundle.json"
    rc = main(["pack", str(demo_repo.contracts), "--service", "service-py", "--repo-root", str(demo_repo.root),
               "--image-packages", str(demo_repo.packages_file), "--out", str(out)])
    assert rc == 1
    assert not out.exists()
    assert any("sitecustomize.py at the repository root" in r.message for r in caplog.records)


def test_pack_never_asks_this_environment_for_its_packages(demo_repo, tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("pack listed the distributions of the environment it runs in")

    monkeypatch.setattr(metadata, "distributions", refuse)
    out = tmp_path / "odcs-bundle.json"
    assert main(["pack", str(demo_repo.contracts), "--service", "service-py", "--repo-root", str(demo_repo.root),
                 "--image-packages", str(demo_repo.packages_file), "--out", str(out)]) == 0


def test_merge_is_gone(contract_repo, tmp_path, capsys):
    out = tmp_path / "contract.yaml"
    with pytest.raises(SystemExit) as exc:
        main(["merge", str(contract_repo / "contracts"), "--service", "s", "--repo-root", str(contract_repo),
              "--out", str(out)])
    assert exc.value.code == 2
    assert "invalid choice: 'merge'" in capsys.readouterr().err
    assert not out.exists()


def _fake_build_bundle(captured):
    def fake(contract_dir, repo_root, service, image_packages, *, dialect=None):
        captured["dialect"] = dialect
        captured["image_packages"] = image_packages
        return {"bundle_version": 1, "service": service, "nodes": []}

    return fake


def test_pack_dialect_flag_threads_to_build_bundle(demo_repo, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(cli_module, "build_bundle", _fake_build_bundle(captured))
    rc = main(["pack", str(demo_repo.contracts), "--service", "s", "--repo-root", str(demo_repo.root),
               "--image-packages", str(demo_repo.packages_file), "--out", str(tmp_path / "b.json"),
               "--dialect", "trino"])
    assert rc == 0
    assert captured["dialect"] == "trino"
    assert captured["image_packages"] == {"pyarrow": "26.0.0"}


def test_pack_dialect_defaults_to_none(demo_repo, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(cli_module, "build_bundle", _fake_build_bundle(captured))
    rc = main(["pack", str(demo_repo.contracts), "--service", "s", "--repo-root", str(demo_repo.root),
               "--image-packages", str(demo_repo.packages_file), "--out", str(tmp_path / "b.json")])
    assert rc == 0
    assert captured["dialect"] is None


def test_pack_creates_a_missing_out_dir(demo_repo, tmp_path):
    out = tmp_path / "nested" / "dir" / "odcs-bundle.json"
    rc = main(["pack", str(demo_repo.contracts), "--service", "service-py", "--repo-root", str(demo_repo.root),
               "--image-packages", str(demo_repo.packages_file), "--out", str(out)])
    assert rc == 0
    assert out.exists()


def test_hash_prints_relation_and_hash(contract_repo, capsys):
    rc = main(
        ["hash", str(contract_repo / "contracts"), "--repo-root", str(contract_repo)]
    )
    assert rc == 0
    line = capsys.readouterr().out.strip()
    rel, h = line.split("\t")
    assert rel == "analytics.t" and h.startswith("sha256:")


def test_hash_prints_one_line_per_node(demo_repo, capsys):
    rc = main(["hash", str(demo_repo.contracts), "--repo-root", str(demo_repo.root)])
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    tables = ["demo_orders_csv", "fx_eur_usd_rates", "py_daily_kpis"]
    assert [line.split("\t")[0] for line in lines] == [f"analytics.{table}" for table in tables]
    for line, table in zip(lines, tables):
        inputs = gather_hash_inputs(demo_repo.node(table), demo_repo.root)
        expected = hash_parts(demo_repo.node(table), inputs.primary, inputs.units)["content_hash"]
        assert line == f"analytics.{table}\t{expected}"


def test_hash_sorts_by_relation_not_by_file_name(tmp_path, capsys):
    contracts = tmp_path / "contracts"
    for schema, table in (("zeta", "a_table"), ("alpha", "z_table")):
        write_contract(
            contracts, schema=schema, table=table, owner="o", schedule="daily",
            criticality="SECONDARY", kind="python-csv", reads={"csv": f"s3://b/{table}.csv"},
            output_columns=[{"name": "id", "type": "INTEGER"}],
        )
    assert main(["hash", str(contracts), "--repo-root", str(tmp_path)]) == 0
    relations = [line.split("\t")[0] for line in capsys.readouterr().out.splitlines()]
    assert relations == ["alpha.z_table", "zeta.a_table"]


def test_hash_prints_nothing_when_a_node_cannot_be_hashed(demo_repo, capsys, caplog):
    (demo_repo.root / "scripts" / "fx_eur_usd_rates.py").unlink()
    rc = main(["hash", str(demo_repo.contracts), "--repo-root", str(demo_repo.root)])
    assert rc == 1
    assert capsys.readouterr().out == ""
    assert any("analytics.fx_eur_usd_rates" in r.message and "script not found" in r.message for r in caplog.records)


def test_hash_dialect_flag_threads_to_the_loader(contract_repo, monkeypatch):
    captured = {}

    def fake_load_contract_dir(path, *, dialect=None):
        captured["dialect"] = dialect
        return []

    monkeypatch.setattr(cli_module, "load_contract_dir", fake_load_contract_dir)
    rc = main(
        [
            "hash",
            str(contract_repo / "contracts"),
            "--repo-root",
            str(contract_repo),
            "--dialect",
            "postgres",
        ]
    )
    assert rc == 0
    assert captured["dialect"] == "postgres"


def test_hash_dialect_defaults_to_none(contract_repo, monkeypatch):
    captured = {}

    def fake_load_contract_dir(path, *, dialect=None):
        captured["dialect"] = dialect
        return []

    monkeypatch.setattr(cli_module, "load_contract_dir", fake_load_contract_dir)
    rc = main(
        ["hash", str(contract_repo / "contracts"), "--repo-root", str(contract_repo)]
    )
    assert rc == 0
    assert captured["dialect"] is None


def test_hash_missing_script_exits_1(contract_repo, caplog):
    write_contract(contract_repo / "contracts", **{**NODE_T, "script": "scripts/nonexistent.py"})

    rc = main(
        ["hash", str(contract_repo / "contracts"), "--repo-root", str(contract_repo)]
    )
    assert rc == 1
    assert any("script not found" in r.message for r in caplog.records)


def test_lint_good_scripts_returns_0(tmp_path):
    # Create a good script directory
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "good.py").write_text("def run(ctx):\n    return ctx.read('ids')\n")

    rc = main(["lint", str(scripts_dir)])
    assert rc == 0


def test_lint_bad_scripts_returns_1(tmp_path, caplog):
    # Create a bad script directory with forbidden import
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "bad.py").write_text("import psycopg2\ndef run(ctx):\n    return None\n")

    rc = main(["lint", str(scripts_dir)])
    assert rc == 1
    assert any("psycopg2" in r.message for r in caplog.records)


def test_lint_nonexistent_path_returns_1(caplog):
    # Nonexistent path should exit 1 with path error, not crash
    rc = main(["lint", "/nonexistent/path/to/script.py"])
    assert rc == 1
    assert any("path does not exist" in r.message for r in caplog.records)


def test_lint_unreadable_file_reports_violation_and_exits_1(tmp_path, caplog):
    """A non-UTF-8 .py file should yield an 'unreadable' violation and exit 1
    instead of raising UnicodeDecodeError."""
    bad_file = tmp_path / "bad.py"
    bad_file.write_bytes(b"# caf\xe9\n")

    rc = main(["lint", str(bad_file)])
    assert rc == 1
    assert any("unreadable" in r.message for r in caplog.records)


def test_main_oserror_backstop_returns_1_no_traceback(demo_repo, tmp_path, caplog):
    """An OSError anywhere in a subcommand is caught by the CLI backstop and turned into
    exit code 1, not a raw traceback. Here --out sits below a file, so its folder cannot exist."""
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    out = blocker / "odcs-bundle.json"

    rc = main(["pack", str(demo_repo.contracts), "--service", "service-py", "--repo-root", str(demo_repo.root),
               "--image-packages", str(demo_repo.packages_file), "--out", str(out)])
    assert rc == 1
    assert any("blocker" in r.message or "Not a directory" in r.message or "Errno" in r.message for r in caplog.records)
    assert blocker.read_text() == "x"


def test_run_success_emits_sentinel_block(harness_repo, monkeypatch, capsys):
    """Test that run subcommand calls harness.run_node and returns its exit code."""
    # Set environment variables for run_node
    monkeypatch.setenv("NODE_ID", "python-node.svc.analytics.t")
    monkeypatch.setenv("TABLE_NAME", "t")
    monkeypatch.setenv("TARGET_SCHEMA", "analytics")
    monkeypatch.setenv("CONTRACT_DIR", str(harness_repo / "contracts"))
    monkeypatch.setenv("APP_ROOT", str(harness_repo))

    # Monkeypatch build_adapter to return FakeWarehouseAdapter
    def fake_build_adapter():
        return FakeWarehouseAdapter({"select id from analytics.a": pa.table({"id": [1]})})

    import continuo_python_runtime.harness
    monkeypatch.setattr(continuo_python_runtime.harness, "build_adapter", fake_build_adapter)

    # Run the command
    rc = main(["run"])
    assert rc == 0

    # Check that exactly one sentinel block was emitted with "success"
    out = capsys.readouterr().out
    assert out.count("===CONTINUO_VALIDATION_RESULT_BEGIN===") == 1
    body = json.loads(out.split("BEGIN===\n")[1].split("\n===CONTINUO")[0])
    assert body["status"] == "success"


def test_validation_op_subcommand_dispatches_runner(monkeypatch):
    """`continuo-runtime validation-op` executes one blue/green validation op —
    the container command continuo's executor sets on validation and schema-op
    pods. (`validate` was taken: it lints contract dirs in domain-repo CI.)"""
    calls = []
    import continuo_python_runtime.validation.runner as vrunner
    monkeypatch.setattr(vrunner, "main", lambda: calls.append("ran"))
    from continuo_python_runtime.cli import main
    assert main(["validation-op"]) == 0
    assert calls == ["ran"]
