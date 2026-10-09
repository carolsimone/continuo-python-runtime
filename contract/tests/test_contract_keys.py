"""ensure_unique_key: the primary-key invariant every upsert checks first."""
import datetime
from decimal import Decimal

import pyarrow as pa
import pytest

from continuo_engine_contract.keys import DuplicateKeyError, ensure_unique_key


def test_duplicate_key_error_is_a_value_error():
    assert issubclass(DuplicateKeyError, ValueError)


def test_a_unique_single_column_key_passes():
    ensure_unique_key(pa.table({"id": [1, 2, 3], "v": ["a", "a", "a"]}), ["id"])


def test_a_unique_composite_key_passes():
    ensure_unique_key(pa.table({"d": [1, 1, 2], "b": ["EUR", "USD", "EUR"]}), ("d", "b"))


def test_empty_and_single_row_tables_pass():
    ensure_unique_key(pa.table({"id": pa.array([], pa.int64())}), ["id"])
    ensure_unique_key(pa.table({"id": [1]}), ["id"])


def test_a_repeated_key_raises_naming_the_key_and_its_count():
    data = pa.table({
        "rate_date": [datetime.date(2026, 10, 8)] * 2 + [datetime.date(2026, 10, 9)],
        "base": ["EUR", "EUR", "EUR"],
        "rate": pa.array([Decimal("1.1")] * 3, pa.decimal128(12, 6)),
    })
    with pytest.raises(
        DuplicateKeyError,
        match=(
            r"^primary key \(rate_date, base\) is not unique in the rows to write: "
            r"\(2026-10-08, EUR\) appears 2 times; nothing was written$"
        ),
    ):
        ensure_unique_key(data, ["rate_date", "base"])


def test_repeated_keys_are_listed_in_order_of_first_appearance_and_capped_at_five():
    ids = [7, 7, 3, 3, 3] + [n for n in range(10, 16) for _ in range(2)]
    with pytest.raises(DuplicateKeyError) as caught:
        ensure_unique_key(pa.table({"id": ids}), ["id"])
    message = str(caught.value)
    assert message.index("(7) appears 2 times") < message.index("(3) appears 3 times")
    assert "(12) appears 2 times" in message
    assert "(13) appears" not in message
    assert "; and 3 more repeated key(s); nothing was written" in message


def test_a_key_column_named_like_the_aggregate_still_works():
    with pytest.raises(DuplicateKeyError, match=r"\(1\) appears 2 times"):
        ensure_unique_key(pa.table({"count_all": [1, 1]}), ["count_all"])


def test_an_empty_key_is_rejected():
    with pytest.raises(ValueError, match="at least one column"):
        ensure_unique_key(pa.table({"id": [1]}), [])


def test_a_key_column_missing_from_the_data_is_rejected():
    with pytest.raises(ValueError, match=r"primary key column\(s\) \['quote'\]") as caught:
        ensure_unique_key(pa.table({"base": ["EUR"]}), ["base", "quote"])
    assert type(caught.value) is ValueError
