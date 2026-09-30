"""TableLayout: fail-closed validation of the duckdb physical-layout vocabulary."""
import pytest

from continuo_duckdb_adapter.domain.identifiers import Identifier
from continuo_duckdb_adapter.domain.layout import PartitionKey, SortKey, TableLayout

TYPES = {"id": "INTEGER", "ts": "TIMESTAMP", "day_col": "DATE", "name": "TEXT"}


def _layout(config, types=TYPES):
    return TableLayout.from_config(config, types)


def test_none_and_empty_config_give_an_empty_layout():
    assert _layout(None).is_empty
    assert _layout({}).is_empty
    assert TableLayout.empty().is_empty


def test_unknown_top_level_key_is_rejected():
    with pytest.raises(ValueError, match="indexes"):
        _layout({"indexes": []})


def test_config_must_be_a_mapping():
    with pytest.raises(ValueError, match="mapping"):
        _layout(["partitioned_by"])


def test_string_entry_is_identity_partitioning():
    layout = _layout({"partitioned_by": ["name"]})
    assert layout.partition_keys == (PartitionKey(Identifier("name")),)


def test_transform_entries():
    layout = _layout({"partitioned_by": [
        {"column": "id", "transform": "bucket", "buckets": 8},
        {"column": "ts", "transform": "month"},
        {"column": "day_col", "transform": "day"},
        {"column": "ts", "transform": "hour"},
        {"column": "ts", "transform": "year"},
    ]})
    assert layout.partition_keys == (
        PartitionKey(Identifier("id"), "bucket", 8),
        PartitionKey(Identifier("ts"), "month"),
        PartitionKey(Identifier("day_col"), "day"),
        PartitionKey(Identifier("ts"), "hour"),
        PartitionKey(Identifier("ts"), "year"),
    )


@pytest.mark.parametrize("bad", [
    "name",                                   # not a list
    [],                                       # empty list
    [7],                                      # entry neither str nor mapping
    [{"column": "missing"}],                  # undeclared column
    [{"column": ""}],                         # empty column
    [{}],                                     # no column
    [{"column": "name", "transform": "week"}],
    [{"column": "name", "transform": "bucket"}],                 # no buckets
    [{"column": "id", "transform": "bucket", "buckets": 0}],
    [{"column": "id", "transform": "bucket", "buckets": True}],
    [{"column": "id", "transform": "bucket", "buckets": "4"}],
    [{"column": "id", "transform": "identity", "buckets": 4}],   # buckets w/o bucket
    [{"column": "id", "transform": "month"}],                    # time transform on INTEGER
    [{"column": "name", "transform": "year"}],                   # time transform on TEXT
    [{"column": "id", "extra": 1}],                              # unknown entry key
    ["name", "name"],                                            # duplicate key
])
def test_bad_partitioned_by_is_rejected(bad):
    with pytest.raises(ValueError):
        _layout({"partitioned_by": bad})


def test_time_transform_type_check_is_skipped_when_the_type_is_unknown():
    layout = _layout({"partitioned_by": [{"column": "id", "transform": "month"}]}, {"id": None})
    assert layout.partition_keys == (PartitionKey(Identifier("id"), "month"),)


def test_column_existence_is_checked_even_when_types_are_unknown():
    with pytest.raises(ValueError, match="nope"):
        _layout({"partitioned_by": ["nope"]}, {"id": None})


def test_sorted_by_defaults_and_options():
    layout = _layout({"sorted_by": [
        "id",
        {"column": "ts", "direction": "DESC", "nulls": "First"},
        {"column": "name", "direction": "asc", "nulls": "last"},
    ]})
    assert layout.sort_keys == (
        SortKey(Identifier("id")),
        SortKey(Identifier("ts"), descending=True, nulls_first=True),
        SortKey(Identifier("name"), descending=False, nulls_first=False),
    )


@pytest.mark.parametrize("bad", [
    "id", [], [7], [{"column": "missing"}], [{}],
    [{"column": "id", "direction": "sideways"}],
    [{"column": "id", "direction": 1}],
    [{"column": "id", "nulls": "middle"}],
    [{"column": "id", "nulls": 0}],
    [{"column": "id", "expr": "id + 1"}],        # free-form expressions are not allowed
    ["id", "id"],                                # duplicate sort column
    [{"column": "id; DROP TABLE x"}],            # not a declared column
])
def test_bad_sorted_by_is_rejected(bad):
    with pytest.raises(ValueError):
        _layout({"sorted_by": bad})


def test_both_keys_together():
    layout = _layout({"partitioned_by": ["name"], "sorted_by": ["id"]})
    assert not layout.is_empty
    assert len(layout.partition_keys) == 1 and len(layout.sort_keys) == 1
