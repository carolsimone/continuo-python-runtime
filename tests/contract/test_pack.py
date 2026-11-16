"""Tests for `continuo-runtime pack`: the bundle of a service's ODCS contracts."""

import hashlib
import json
from hashlib import sha256
from importlib import metadata
from pathlib import Path

import pytest

from continuo_python_runtime.contract import pack as pack_module
from continuo_python_runtime.contract.pack import (
    build_bundle,
    image_packages_from,
    write_bundle,
)
from continuo_python_runtime.errors import ContractError
from continuo_python_runtime.hashing import shared_code_hash
from tests.conftest import DEMO_SCRIPTS, NODE_T
from tests.odcs_builder import contract_text, write_contract
from tests.test_hashing import RELEASED_0_12_0

NODE_KEYS = {"path", "text", "source_hash", "shared_code_hash"}
PACKAGES = {"pyarrow": "26.0.0", "continuo-python-runtime": "0.13.0"}


def bundle_of(repo, service="s", packages=None, **kwargs):
    """The bundle of the contracts in ``<repo>/contracts``."""
    return build_bundle(repo / "contracts", repo, service, PACKAGES if packages is None else packages, **kwargs)


def only_node(repo):
    (node,) = bundle_of(repo)["nodes"]
    return node


def write_node(repo, script="scripts/node.py", **fields):
    """Write ``contracts/<table>.odcs.yaml``: the node ``analytics.t`` that runs ``script``."""
    return write_contract(repo / "contracts", **{**NODE_T, "script": script, **fields})


def write_script(repo, relative, text="def run(ctx):\n    return ctx.read('ids')\n"):
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# --- the bundle ---------------------------------------------------------------------


def test_the_bundle_has_the_documented_shape(contract_repo):
    bundle = bundle_of(contract_repo, "marketing-py")
    assert bundle == {
        "bundle_version": 1,
        "service": "marketing-py",
        "producer": {
            "continuo_python_runtime": metadata.version("continuo-python-runtime"),
            "continuo_contracts": metadata.version("continuo-contracts"),
        },
        "image_packages": PACKAGES,
        "nodes": [
            {
                "path": "contracts/t.odcs.yaml",
                "text": (contract_repo / "contracts" / "t.odcs.yaml").read_text(),
                "source_hash": sha256((contract_repo / "scripts" / "t.py").read_bytes()).hexdigest(),
                "shared_code_hash": "",
            }
        ],
    }


def test_a_node_carries_exactly_four_keys(contract_repo):
    assert set(only_node(contract_repo)) == NODE_KEYS


def test_the_hash_parts_are_bare_hex(tmp_path):
    write_script(tmp_path, "scripts/helper.py", "X = 1\n")
    write_script(tmp_path, "scripts/node.py", "import helper\n\n\ndef run(ctx):\n    return ctx.read('ids')\n")
    write_node(tmp_path)
    node = only_node(tmp_path)
    for part in ("source_hash", "shared_code_hash"):
        assert len(node[part]) == 64 and int(node[part], 16) >= 0


def test_the_demo_bundle_carries_the_hash_parts_runtime_0_12_0_wrote(demo_repo):
    nodes = build_bundle(demo_repo.contracts, demo_repo.root, "service-py", {})["nodes"]
    assert [(n["path"].rsplit("/", 1)[1].removesuffix(".odcs.yaml"), n["source_hash"], n["shared_code_hash"]) for n in nodes] == [
        (table, RELEASED_0_12_0[table]["source_hash"], RELEASED_0_12_0[table]["shared_code_hash"])
        for table in ("demo_orders_csv", "fx_eur_usd_rates", "py_daily_kpis")
    ]
    assert nodes[2]["shared_code_hash"] == shared_code_hash({"scripts/kpi_math.py": DEMO_SCRIPTS["scripts/kpi_math.py"]})


def test_nodes_are_sorted_by_path_and_each_holds_its_file_as_written(demo_repo):
    nodes = build_bundle(demo_repo.contracts, demo_repo.root, "service-py", {})["nodes"]
    assert [n["path"] for n in nodes] == [
        "contracts/demo_orders_csv.odcs.yaml",
        "contracts/fx_eur_usd_rates.odcs.yaml",
        "contracts/py_daily_kpis.odcs.yaml",
    ]
    for node in nodes:
        assert node["text"] == (demo_repo.root / node["path"]).read_text(encoding="utf-8")


def test_a_path_is_the_posix_path_below_the_repository_root(tmp_path):
    write_script(tmp_path, "svc/scripts/node.py")
    write_contract(tmp_path / "svc" / "contracts", **{**NODE_T, "script": "svc/scripts/node.py"})
    bundle = build_bundle(tmp_path / "svc" / "contracts", tmp_path, "s", {})
    assert [n["path"] for n in bundle["nodes"]] == ["svc/contracts/t.odcs.yaml"]


def test_a_path_does_not_depend_on_how_the_folders_are_spelled(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    write_script(repo, "scripts/node.py")
    write_node(repo)
    expected = build_bundle(repo / "contracts", repo, "s", {})
    monkeypatch.chdir(repo)
    assert build_bundle(Path("contracts"), Path("."), "s", {}) == expected
    alias = tmp_path / "alias"
    alias.symlink_to(repo)
    assert build_bundle(repo / "contracts", alias, "s", {}) == expected
    assert [n["path"] for n in expected["nodes"]] == ["contracts/t.odcs.yaml"]


def test_contracts_outside_the_repository_root_are_refused(tmp_path):
    repo = tmp_path / "repo"
    write_script(repo, "scripts/node.py")
    write_contract(tmp_path / "elsewhere", **{**NODE_T, "script": "scripts/node.py"})
    with pytest.raises(ContractError, match=r"elsewhere.*t\.odcs\.yaml.*is not inside the repository root"):
        build_bundle(tmp_path / "elsewhere", repo, "s", {})


def test_the_text_is_the_file_byte_for_byte(tmp_path):
    write_script(tmp_path, "scripts/node.py")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    text = contract_text(**{**NODE_T, "script": "scripts/node.py", "description": "café"})
    (contracts / "t.odcs.yaml").write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    assert only_node(tmp_path)["text"] == text.replace("\n", "\r\n")


def test_no_in_repo_import_means_an_empty_shared_code_hash(contract_repo):
    assert only_node(contract_repo)["shared_code_hash"] == ""


def test_editing_a_shared_helper_moves_only_the_importing_nodes_shared_code_hash(tmp_path):
    write_script(tmp_path, "scripts/helpers.py", "def add(a, b):\n    return a + b\n")
    write_script(tmp_path, "scripts/with_helper.py", "import scripts.helpers\n\n\ndef run(ctx):\n    return ctx.read('ids')\n")
    write_script(tmp_path, "scripts/without_helper.py")
    write_node(tmp_path, "scripts/with_helper.py", table="with_helper")
    write_node(tmp_path, "scripts/without_helper.py", table="without_helper")

    def by_table():
        return {n["path"].split("/")[1].split(".")[0]: n for n in bundle_of(tmp_path)["nodes"]}

    before = by_table()
    assert before["with_helper"]["shared_code_hash"] != ""
    assert before["without_helper"]["shared_code_hash"] == ""

    helpers = tmp_path / "scripts" / "helpers.py"
    helpers.write_text(helpers.read_text().replace("a + b", "a + b "))

    after = by_table()
    assert after["with_helper"]["shared_code_hash"] != before["with_helper"]["shared_code_hash"]
    assert after["with_helper"]["source_hash"] == before["with_helper"]["source_hash"]
    assert after["without_helper"] == before["without_helper"]


def test_reformatting_a_contract_moves_its_text_and_no_hash_part(contract_repo):
    before = only_node(contract_repo)
    write_node(
        contract_repo, "scripts/t.py", owner="someone-else", schedule="hourly", criticality="CORE",
        description="reworded", reads={"ids": "  select id from analytics.a\r\n"},
    )
    after = only_node(contract_repo)
    assert after["text"] != before["text"]
    assert (after["source_hash"], after["shared_code_hash"]) == (before["source_hash"], before["shared_code_hash"])


def test_each_closure_file_is_folded_with_its_repo_path(tmp_path):
    write_script(tmp_path, "scripts/helpers.py", "def add(a, b):\n    return a + b\n")
    write_script(tmp_path, "scripts/node.py", "import scripts.helpers\n\n\ndef run(ctx):\n    return ctx.read('ids')\n")
    write_node(tmp_path)
    node = only_node(tmp_path)
    assert node["source_hash"] == hashlib.sha256((tmp_path / "scripts" / "node.py").read_bytes()).hexdigest()
    assert node["shared_code_hash"] == shared_code_hash({"scripts/helpers.py": (tmp_path / "scripts" / "helpers.py").read_bytes()})


def test_swapping_two_imported_files_moves_the_shared_code_hash(tmp_path):
    write_script(tmp_path, "lib/__init__.py", "")
    write_script(tmp_path, "lib/p.py", "def value():\n    return 7\n")
    write_script(tmp_path, "lib/q.py", "def value():\n    return -7\n")
    write_script(tmp_path, "scripts/node.py", "from lib import p, q\n\n\ndef run(ctx):\n    return ctx.read('ids')\n")
    write_node(tmp_path)
    before = only_node(tmp_path)
    p, q = (tmp_path / "lib" / "p.py").read_bytes(), (tmp_path / "lib" / "q.py").read_bytes()
    (tmp_path / "lib" / "p.py").write_bytes(q)
    (tmp_path / "lib" / "q.py").write_bytes(p)
    after = only_node(tmp_path)
    assert after["source_hash"] == before["source_hash"]
    assert after["shared_code_hash"] != before["shared_code_hash"]


def test_a_csv_node_hashes_its_uri_and_needs_no_script(tmp_path):
    uri = "s3://drops/orders.csv"
    write_contract(
        tmp_path / "contracts", schema="analytics", table="orders_csv", owner="team", schedule="daily",
        criticality="SECONDARY", kind="python-csv", reads={"csv": uri},
        output_columns=[{"name": "order_id", "type": "INTEGER", "nullable": False}],
    )
    (node,) = bundle_of(tmp_path)["nodes"]
    assert node["source_hash"] == sha256(uri.encode()).hexdigest()
    assert node["shared_code_hash"] == ""


def test_a_python_api_node_hashes_its_script(tmp_path):
    script = write_script(tmp_path, "scripts/fx.py", "import pyarrow as pa\n\n\ndef run(ctx):\n    return pa.table({'rate': [1.0]})\n")
    write_contract(
        tmp_path / "contracts", schema="analytics", table="fx", owner="m", schedule="daily",
        criticality="SECONDARY", kind="python-api", script="scripts/fx.py", secret_ref="continuo-api-fx",
        output_columns=[{"name": "rate", "type": "DOUBLE PRECISION"}],
    )
    (node,) = bundle_of(tmp_path)["nodes"]
    assert node["source_hash"] == sha256(script.read_bytes()).hexdigest()


# --- the script's path --------------------------------------------------------------


def test_a_script_that_is_missing_is_refused(contract_repo):
    (contract_repo / "scripts" / "t.py").unlink()
    with pytest.raises(ContractError, match="scripts/t.py"):
        bundle_of(contract_repo)


def test_an_absolute_script_path_is_refused(tmp_path):
    write_script(tmp_path, "scripts/t.py")
    write_node(tmp_path, "/etc/passwd")
    with pytest.raises(ContractError, match="must be relative"):
        bundle_of(tmp_path)


def test_an_absolute_script_path_inside_the_repository_is_refused(tmp_path):
    script = write_script(tmp_path, "scripts/t.py")
    write_node(tmp_path, str(script.resolve()))
    with pytest.raises(ContractError, match="must be relative"):
        bundle_of(tmp_path)


def test_a_script_path_with_a_parent_part_is_refused_naming_the_file(tmp_path):
    write_script(tmp_path, "scripts/t.py")
    write_node(tmp_path, "../outside.py")
    with pytest.raises(ContractError, match=r"t\.odcs\.yaml: .*`script` is '\.\./outside\.py'"):
        bundle_of(tmp_path)


def test_a_script_that_is_a_link_out_of_the_repository_is_refused(tmp_path):
    (tmp_path / "outside.py").write_text("def run(ctx):\n    return None\n")
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "t.py").symlink_to(tmp_path / "outside.py")
    write_node(repo, "scripts/t.py")
    with pytest.raises(ContractError, match="escapes the repository root"):
        bundle_of(repo)


def test_a_script_path_that_names_a_directory_is_refused(tmp_path):
    (tmp_path / "scripts" / "subdir").mkdir(parents=True)
    write_node(tmp_path, "scripts/subdir")
    with pytest.raises(ContractError, match="script not found"):
        bundle_of(tmp_path)


def test_a_dynamic_import_in_the_script_is_refused(tmp_path):
    write_script(tmp_path, "scripts/node.py", "def run(ctx):\n    exec(\"pass\")\n    return ctx.read('ids')\n")
    write_node(tmp_path)
    with pytest.raises(ContractError):
        bundle_of(tmp_path)


# --- the closure lint ---------------------------------------------------------------


def test_a_closure_member_that_imports_a_driver_is_refused(tmp_path):
    write_script(tmp_path, "lib/shared.py", "import psycopg2\n")
    write_script(tmp_path, "scripts/node.py", "from lib.shared import fetch_it\n\n\ndef run(ctx):\n    return ctx.read('ids')\n")
    write_node(tmp_path)
    with pytest.raises(ContractError) as exc:
        bundle_of(tmp_path)
    assert "analytics.t" in str(exc.value)
    assert "lib/shared.py:1: forbidden warehouse driver import 'psycopg2'" in str(exc.value)


def test_a_closure_member_with_a_sql_string_literal_is_refused(tmp_path):
    write_script(tmp_path, "lib/shared.py", "def query():\n    return 'select secret from analytics.private'\n")
    write_script(tmp_path, "scripts/node.py", "from lib.shared import query\n\n\ndef run(ctx):\n    return ctx.read('ids')\n")
    write_node(tmp_path)
    with pytest.raises(ContractError, match=r"lib/shared\.py:2: SQL string literal"):
        bundle_of(tmp_path)


def test_a_closure_member_with_a_data_access_call_is_refused(tmp_path):
    write_script(tmp_path, "lib/shared.py", "def fetch_it(conn):\n    return conn.execute('noop')\n")
    write_script(tmp_path, "scripts/node.py", "from lib.shared import fetch_it\n\n\ndef run(ctx):\n    return ctx.read('ids')\n")
    write_node(tmp_path)
    with pytest.raises(ContractError, match=r"lib/shared\.py:2: forbidden data-access call 'execute'"):
        bundle_of(tmp_path)


def test_the_violations_of_two_closure_members_come_in_one_error(tmp_path):
    write_script(tmp_path, "lib/shared.py", "import psycopg2\n")
    write_script(tmp_path, "lib/other.py", "def query():\n    return 'select secret from analytics.private'\n")
    write_script(
        tmp_path, "scripts/node.py",
        "from lib.shared import fetch_it\nfrom lib.other import query\n\n\ndef run(ctx):\n    return ctx.read('ids')\n",
    )
    write_node(tmp_path)
    with pytest.raises(ContractError) as exc:
        bundle_of(tmp_path)
    assert "lib/shared.py:1: forbidden warehouse driver import 'psycopg2'" in str(exc.value)
    assert "lib/other.py:2: SQL string literal" in str(exc.value)


def test_a_script_with_a_lint_violation_is_refused(tmp_path):
    write_script(tmp_path, "scripts/node.py", "import psycopg2\n\n\ndef run(ctx):\n    return ctx.read('ids')\n")
    write_node(tmp_path)
    with pytest.raises(ContractError, match=r"scripts/node\.py:1: forbidden warehouse driver import 'psycopg2'"):
        bundle_of(tmp_path)


def test_a_python_api_script_is_held_to_the_closure_lint(tmp_path):
    write_script(tmp_path, "scripts/fx.py", "import psycopg2\n\n\ndef run(ctx):\n    return None\n")
    write_contract(
        tmp_path / "contracts", schema="analytics", table="fx", owner="m", schedule="daily",
        criticality="SECONDARY", kind="python-api", script="scripts/fx.py",
        output_columns=[{"name": "rate", "type": "DOUBLE PRECISION"}],
    )
    with pytest.raises(ContractError, match="lint violations"):
        bundle_of(tmp_path)


def test_a_closure_member_with_a_latin1_coding_cookie_is_a_contract_error(tmp_path):
    """`ast.parse(bytes)` honours the cookie, the lint decodes as UTF-8: the failure must be a ContractError."""
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "shared.py").write_bytes("# -*- coding: latin-1 -*-\n# caf\xe9\nX = 1\n".encode("latin-1"))
    write_script(tmp_path, "scripts/node.py", "from lib.shared import X\n\n\ndef run(ctx):\n    return ctx.read('ids')\n")
    write_node(tmp_path)
    with pytest.raises(ContractError) as exc:
        bundle_of(tmp_path)
    assert "analytics.t" in str(exc.value)
    assert "lib/shared.py" in str(exc.value)


def test_a_closure_member_with_a_utf8_bom_is_packed(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "shared.py").write_bytes(b"\xef\xbb\xbfX = 1\n")
    write_script(tmp_path, "scripts/node.py", "from lib.shared import X\n\n\ndef run(ctx):\n    return ctx.read('ids')\n")
    write_node(tmp_path)
    assert only_node(tmp_path)["shared_code_hash"] != ""


def test_a_sitecustomize_below_the_repository_root_is_caught_by_the_closure_lint(tmp_path):
    write_script(tmp_path, "scripts/sitecustomize.py", "X = 1\n")
    write_script(tmp_path, "scripts/node.py", "import scripts.sitecustomize\n\n\ndef run(ctx):\n    return ctx.read('ids')\n")
    write_node(tmp_path)
    with pytest.raises(ContractError, match="unhashed code construct 'sitecustomize'"):
        bundle_of(tmp_path)


def test_a_closure_member_that_runs_code_with_runpy_is_refused(tmp_path):
    write_script(tmp_path, "scripts/helpers.py", "import runpy\n\n\ndef load(path):\n    return runpy.run_path(path)\n")
    write_script(tmp_path, "scripts/node.py", "from scripts.helpers import load\n\n\ndef run(ctx):\n    return ctx.read('ids')\n")
    write_node(tmp_path)
    with pytest.raises(ContractError, match="unhashed code construct 'runpy'"):
        bundle_of(tmp_path)


@pytest.mark.parametrize("name", ["sitecustomize.py", "usercustomize.py"])
def test_a_startup_hook_at_the_repository_root_is_refused(contract_repo, name):
    (contract_repo / name).write_text("X = 1\n")
    with pytest.raises(ContractError, match=f"{name} at the repository root"):
        bundle_of(contract_repo)


# --- the assets ---------------------------------------------------------------------


def assets_repo(root, assets):
    """A repository with one python-node that declares *assets*, and the file data/ref.json."""
    write_script(root, "scripts/node.py")
    (root / "data").mkdir()
    (root / "data" / "ref.json").write_text('{"rate": 1}')
    write_node(root, assets=assets)
    return root


def test_editing_an_asset_moves_only_the_shared_code_hash(tmp_path):
    repo = assets_repo(tmp_path, ["data/ref.json"])
    before = only_node(repo)
    (repo / "data" / "ref.json").write_text('{"rate": 2}')
    after = only_node(repo)
    assert after["source_hash"] == before["source_hash"]
    assert after["shared_code_hash"] != before["shared_code_hash"]


def test_declaring_an_asset_moves_the_shared_code_hash(tmp_path):
    without = only_node(assets_repo(tmp_path / "a", None))
    declared = only_node(assets_repo(tmp_path / "b", ["data/ref.json"]))
    assert declared["source_hash"] == without["source_hash"]
    assert declared["shared_code_hash"] != without["shared_code_hash"]


@pytest.mark.parametrize("content", [b"\x00\xff", b"not python (\n"], ids=["binary", "invalid-python-text"])
def test_an_asset_that_is_not_python_is_packed(tmp_path, content):
    """Assets are hashed as bytes and never linted or parsed."""
    repo = assets_repo(tmp_path, ["data/blob.bin"])
    (repo / "data" / "blob.bin").write_bytes(content)
    assert only_node(repo)["shared_code_hash"] == shared_code_hash({"data/blob.bin": content})


def test_an_asset_that_is_also_imported_is_one_shared_unit(tmp_path):
    repo = assets_repo(tmp_path, ["scripts/helper.py"])
    write_script(repo, "scripts/helper.py", "RATE = 1\n")
    write_script(repo, "scripts/node.py", "import helper\n\n\ndef run(ctx):\n    return ctx.read('ids')\n")
    assert only_node(repo)["shared_code_hash"] == shared_code_hash({"scripts/helper.py": b"RATE = 1\n"})


@pytest.mark.parametrize(("prepare", "match"), [
    (lambda repo: (repo / "data" / "ref.json").unlink(), "asset not found: data/ref.json"),
    (lambda repo: ((repo / "data" / "ref.json").unlink(), (repo / "data" / "ref.json").mkdir()), "is not a file"),
    (lambda repo: ((repo / "data" / "ref.json").unlink(),
                   (repo / "data" / "ref.json").symlink_to(repo.parent / "outside.json")), "escapes the repository root"),
])
def test_an_asset_that_cannot_be_read_is_refused(tmp_path, prepare, match):
    (tmp_path / "outside.json").write_text("{}")
    repo = assets_repo(tmp_path / "repo", ["data/ref.json"])
    prepare(repo)
    with pytest.raises(ContractError, match=match):
        bundle_of(repo)


# --- the contracts ------------------------------------------------------------------


def test_a_contract_of_another_service_is_refused_naming_the_file(tmp_path):
    write_script(tmp_path, "scripts/node.py")
    write_node(tmp_path, service="other")
    with pytest.raises(ContractError) as exc:
        bundle_of(tmp_path, "s")
    assert str(exc.value).startswith("contracts/t.odcs.yaml: ")
    assert "'other'" in str(exc.value) and "'s'" in str(exc.value)


def test_a_contract_that_names_the_service_is_packed(tmp_path):
    write_script(tmp_path, "scripts/node.py")
    write_node(tmp_path, service="s")
    assert only_node(tmp_path)["path"] == "contracts/t.odcs.yaml"


def test_every_issue_of_every_file_is_listed(tmp_path):
    write_script(tmp_path, "scripts/node.py")
    write_node(tmp_path, service="other")
    write_node(tmp_path, table="u", service="another")
    with pytest.raises(ContractError) as exc:
        bundle_of(tmp_path, "s")
    lines = str(exc.value).splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("contracts/t.odcs.yaml: ") and lines[1].startswith("contracts/u.odcs.yaml: ")


def test_a_contract_the_loader_refuses_is_refused(tmp_path):
    write_script(tmp_path, "scripts/node.py")
    write_node(tmp_path, reads={"ids": "select id from orders"})
    with pytest.raises(ContractError, match="without its schema"):
        bundle_of(tmp_path)


def test_a_config_number_beyond_two_to_the_53_is_refused_naming_the_file(tmp_path):
    write_script(tmp_path, "scripts/node.py")
    write_node(tmp_path, config={"n": 2**53})
    with pytest.raises(ContractError, match=r"t\.odcs\.yaml"):
        bundle_of(tmp_path)


def test_the_dialect_decides_which_reads_parse(tmp_path):
    write_script(tmp_path, "scripts/node.py")
    write_node(tmp_path, reads={"ids": "select id from analytics.a where a ~ 'x'"})
    with pytest.raises(ContractError):
        bundle_of(tmp_path)
    assert bundle_of(tmp_path, dialect="postgres")["nodes"][0]["path"] == "contracts/t.odcs.yaml"


# --- write_bundle -------------------------------------------------------------------


def test_the_bundle_is_written_as_sorted_indented_utf8_json_and_a_newline(tmp_path):
    doc = {"b": "café", "a": {"z": 1, "y": [1, 2]}}
    out = tmp_path / "odcs-bundle.json"
    write_bundle(doc, out)
    expected = json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    assert out.read_bytes() == expected.encode("utf-8")
    assert "café".encode("utf-8") in out.read_bytes()


def test_the_out_folder_is_created_when_missing(tmp_path):
    out = tmp_path / "does" / "not" / "exist" / "odcs-bundle.json"
    write_bundle({"a": 1}, out)
    assert json.loads(out.read_text()) == {"a": 1}


def test_the_file_is_written_beside_its_destination_and_renamed(tmp_path, monkeypatch):
    out = tmp_path / "odcs-bundle.json"
    renames = []
    real_replace = pack_module.os.replace

    def spy(source, target):
        renames.append((source, target))
        real_replace(source, target)

    monkeypatch.setattr(pack_module.os, "replace", spy)
    write_bundle({"a": 1}, out)
    ((source, target),) = renames
    assert Path(target) == out and Path(source).parent == out.parent and Path(source) != out
    assert list(tmp_path.iterdir()) == [out]


def test_a_failed_rename_leaves_no_file(tmp_path, monkeypatch):
    out = tmp_path / "odcs-bundle.json"

    def boom(source, target):
        raise PermissionError("denied")

    monkeypatch.setattr(pack_module.os, "replace", boom)
    with pytest.raises(PermissionError):
        write_bundle({"a": 1}, out)
    assert list(tmp_path.iterdir()) == []


def test_a_failed_write_keeps_the_file_that_was_there(tmp_path, monkeypatch):
    out = tmp_path / "odcs-bundle.json"
    out.write_text("old\n")

    def boom(source, target):
        raise PermissionError("denied")

    monkeypatch.setattr(pack_module.os, "replace", boom)
    with pytest.raises(PermissionError):
        write_bundle({"a": 1}, out)
    assert [p.name for p in tmp_path.iterdir()] == ["odcs-bundle.json"]
    assert out.read_text() == "old\n"


def test_a_document_that_is_not_json_leaves_no_file(tmp_path):
    out = tmp_path / "odcs-bundle.json"
    with pytest.raises(TypeError):
        write_bundle({"a": {1, 2}}, out)
    assert list(tmp_path.iterdir()) == []


def test_a_new_file_gets_the_permissions_of_a_file_written_in_place(tmp_path):
    out = tmp_path / "odcs-bundle.json"
    reference = tmp_path / "reference"
    reference.write_text("x")
    write_bundle({"a": 1}, out)
    assert out.stat().st_mode == reference.stat().st_mode


# --- image_packages_from ------------------------------------------------------------


def test_the_packages_come_from_the_file(tmp_path):
    file = tmp_path / "packages.json"
    file.write_text(json.dumps(PACKAGES))
    assert image_packages_from(image=None, image_packages=file) == PACKAGES


@pytest.mark.parametrize(("text", "match"), [
    ("not json", "is not JSON"),
    ('["pyarrow"]', "must be one JSON object of name to version strings"),
    ('{"pyarrow": 26}', "must be one JSON object of name to version strings"),
    ('{"pyarrow": null}', "must be one JSON object of name to version strings"),
])
def test_a_package_file_that_is_not_an_object_of_strings_is_refused(tmp_path, text, match):
    file = tmp_path / "packages.json"
    file.write_text(text)
    with pytest.raises(ContractError, match=match) as exc:
        image_packages_from(image=None, image_packages=file)
    assert str(file) in str(exc.value)


def test_a_package_file_that_is_missing_is_refused_naming_it(tmp_path):
    with pytest.raises(ContractError, match="missing.json"):
        image_packages_from(image=None, image_packages=tmp_path / "missing.json")


def test_the_packages_come_from_the_image(fake_docker):
    docker = fake_docker(stdout=json.dumps(PACKAGES, sort_keys=True) + "\n")
    assert image_packages_from(image="registry/svc:1", image_packages=None) == PACKAGES
    assert docker.calls == ["run", "--rm", "--entrypoint", "continuo-runtime", "registry/svc:1", "packages"]


def test_no_docker_is_a_contract_error_naming_the_image(no_docker):
    with pytest.raises(ContractError) as exc:
        image_packages_from(image="registry/svc:1", image_packages=None)
    assert "registry/svc:1" in str(exc.value) and "docker" in str(exc.value)


def test_an_image_without_the_packages_command_is_refused(fake_docker):
    fake_docker(stderr="continuo-runtime: error: invalid choice: 'packages'\n", exit_code=2)
    with pytest.raises(ContractError) as exc:
        image_packages_from(image="registry/svc:1", image_packages=None)
    message = str(exc.value)
    assert "registry/svc:1" in message
    assert "the image has no `packages` command: build it on continuo-python-runtime 0.13.0 or later" in message
    assert "invalid choice: 'packages'" in message


def test_a_docker_failure_is_reported_with_its_own_words(fake_docker):
    fake_docker(stderr="Unable to find image 'registry/svc:1' locally\n", exit_code=125)
    with pytest.raises(ContractError) as exc:
        image_packages_from(image="registry/svc:1", image_packages=None)
    assert "Unable to find image" in str(exc.value)
    assert "has no `packages` command" not in str(exc.value)


@pytest.mark.parametrize("stdout", ["", "not json\n", '["pyarrow"]\n', '{"pyarrow": 26}\n', '{"a": "1"}\nextra\n'])
def test_image_output_that_is_not_an_object_of_strings_is_refused(fake_docker, stdout):
    fake_docker(stdout=stdout)
    with pytest.raises(ContractError, match="registry/svc:1"):
        image_packages_from(image="registry/svc:1", image_packages=None)


@pytest.mark.parametrize("kwargs", [{}, {"image": "i", "image_packages": "f"}])
def test_exactly_one_package_source_is_needed(kwargs, tmp_path):
    if "image_packages" in kwargs:
        kwargs = {**kwargs, "image_packages": tmp_path / "f"}
    with pytest.raises(ContractError, match="records the package list of the image"):
        image_packages_from(**{"image": None, "image_packages": None, **kwargs})
