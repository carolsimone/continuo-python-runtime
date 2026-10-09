"""The published wheel ships the conformance suite and declares the testing extra."""
import re
import subprocess
import zipfile
from pathlib import Path

CONTRACT_ROOT = Path(__file__).resolve().parent.parent  # contract/


def test_wheel_ships_the_conformance_suite_and_the_testing_extra(tmp_path):
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path), str(CONTRACT_ROOT)],
        check=True,
        capture_output=True,
    )
    (wheel,) = tmp_path.glob("continuo_engine_contract-*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        (metadata_name,) = [n for n in names if n.endswith(".dist-info/METADATA")]
        metadata = archive.read(metadata_name).decode()
    assert "continuo_engine_contract/conformance.py" in names
    assert "continuo_engine_contract/keys.py" in names
    assert "Provides-Extra: testing" in metadata
    assert re.search(r"Requires-Dist: pytest==9\.1\.1; extra == ['\"]testing['\"]", metadata)
    assert re.search(r"Requires-Dist: pyarrow==25\.0\.1; extra == ['\"]testing['\"]", metadata)
