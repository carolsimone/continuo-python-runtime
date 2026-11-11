"""Unit tests for the validation-result contract helper."""
import json

import pytest

from continuo_engine_contract import result


def test_sentinel_markers_are_the_wire_contract():
    """Pin the sentinel strings to their literal wire values.

    The Go side (pkg/validationresult) pins the same literals independently, so an
    edit to either marker on either side fails its own suite — the cross-language
    guard cannot be silently defeated by editing only the Python constant.
    """
    assert result.SENTINEL_BEGIN == "===CONTINUO_VALIDATION_RESULT_BEGIN==="
    assert result.SENTINEL_END == "===CONTINUO_VALIDATION_RESULT_END==="


def _extract(block: str) -> dict:
    lines = block.splitlines()
    assert lines[0] == result.SENTINEL_BEGIN
    assert lines[-1] == result.SENTINEL_END
    return json.loads(lines[1])


def test_result_block_is_sentinel_framed_single_line_json():
    """A populated block round-trips through the sentinel framing intact."""
    block = result.result_block(status="error", message="boom", unique_id="model.svc.x")
    assert _extract(block) == {
        "schema_version": 1, "status": "error", "message": "boom",
        "failures": 0, "unique_id": "model.svc.x",
    }
    assert len(block.splitlines()) == 3


def test_result_block_success_defaults():
    """Omitted message/failures default to empty string and zero."""
    doc = _extract(result.result_block(status="success"))
    assert doc["status"] == "success"
    assert doc["message"] == ""
    assert doc["failures"] == 0


def test_result_block_without_a_reason_keeps_its_frozen_bytes():
    assert result.result_block("error", "boom", 1, "a.b") == (
        result.SENTINEL_BEGIN + "\n"
        + '{"schema_version":1,"status":"error","message":"boom","failures":1,"unique_id":"a.b"}'
        + "\n" + result.SENTINEL_END
    )


def test_result_block_writes_a_reason_only_when_given():
    assert "reason" not in _extract(result.result_block("error", "boom"))
    doc = _extract(result.result_block("error", "boom", reason="image_contract_mismatch"))
    assert list(doc)[-1] == "reason" and doc["reason"] == "image_contract_mismatch"


def test_result_block_writes_op_fields_after_the_standard_keys():
    doc = _extract(result.result_block("success", unique_id="a.b", fields={"op": "probe_views", "views": []}))
    assert doc == {"schema_version": 1, "status": "success", "message": "", "failures": 0,
                   "unique_id": "a.b", "op": "probe_views", "views": []}


def test_result_block_writes_fields_after_the_reason_in_the_order_given():
    doc = _extract(result.result_block("error", reason="r", fields={"z": 1, "a": 2}))
    assert list(doc)[-3:] == ["reason", "z", "a"]


@pytest.mark.parametrize("key", ["schema_version", "status", "message", "failures", "unique_id", "reason"])
def test_result_block_refuses_a_field_that_shadows_a_standard_key(key):
    with pytest.raises(ValueError, match=key):
        result.result_block("success", fields={key: "x"})
