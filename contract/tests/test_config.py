"""Unit tests for the physical-layout config guard."""
import pytest

from continuo_engine_contract.config import ensure_known_keys


def test_empty_config_is_accepted_even_with_no_known_keys():
    """An absent layout block is the common case and must never raise."""
    ensure_known_keys({}, (), "postgres")


def test_known_keys_are_accepted():
    """Every key inside the engine's vocabulary passes."""
    ensure_known_keys({"indexes": [], "extra": 1}, ("indexes", "extra"), "postgres")


def test_unknown_key_is_rejected_naming_key_engine_and_vocabulary():
    """The message must tell the author what they wrote and what the engine knows."""
    with pytest.raises(ValueError) as exc:
        ensure_known_keys({"sortkey": ["id"]}, ("indexes",), "postgres")
    message = str(exc.value)
    assert "'sortkey'" in message
    assert "'postgres'" in message
    assert "indexes" in message


def test_multiple_unknown_keys_are_reported_together_and_sorted():
    """All authoring errors surface in one gate failure, deterministically ordered."""
    with pytest.raises(ValueError) as exc:
        ensure_known_keys({"zeta": 1, "alpha": 2}, ("indexes",), "postgres")
    message = str(exc.value)
    assert message.index("'alpha'") < message.index("'zeta'")


def test_empty_known_set_renders_as_none():
    """An engine with no vocabulary yet still produces a readable message."""
    with pytest.raises(ValueError, match=r"\(none\)"):
        ensure_known_keys({"indexes": []}, (), "trino")


@pytest.mark.parametrize(
    "bad", [[], "indexes", 3, None, True], ids=["list", "str", "int", "none", "bool"]
)
def test_non_mapping_config_is_rejected(bad):
    """Config must be a mapping; a list or scalar is an authoring error, not a default."""
    with pytest.raises(ValueError, match="must be a mapping"):
        ensure_known_keys(bad, ("indexes",), "postgres")


def test_where_label_appears_in_both_messages():
    """Nested callers relabel the subject so the error points at the right block."""
    with pytest.raises(ValueError, match="index entry must be a mapping"):
        ensure_known_keys([], ("columns",), "postgres", where="index entry")
    with pytest.raises(ValueError, match="unrecognized index entry key"):
        ensure_known_keys({"nope": 1}, ("columns",), "postgres", where="index entry")
