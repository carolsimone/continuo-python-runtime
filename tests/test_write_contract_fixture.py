"""The repository-root `write_contract` fixture, which the adapters' integration tests build their contracts with."""
from continuo_python_runtime.contract.loader import load_contract_dir


def test_the_fixture_writes_the_contract_file_the_loader_reads(write_contract, tmp_path):
    path = write_contract(
        tmp_path / "contracts",
        schema="analytics", table="t", owner="m", schedule="daily", criticality="SECONDARY",
        script="scripts/t.py",
        reads={"ids": "select id from analytics.a"},
        output_columns=[{"name": "id", "type": "INTEGER", "nullable": False}],
    )
    assert path == tmp_path / "contracts" / "t.odcs.yaml"
    (node,) = load_contract_dir(tmp_path / "contracts")
    assert (node.schema, node.table, node.owner) == ("analytics", "t", "m")
    assert node.reads == {"ids": "select id from analytics.a"}
