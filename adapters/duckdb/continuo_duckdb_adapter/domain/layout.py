"""Physical layout of a DuckLake table: partitioning and sort order.

This is the duckdb adapter's ``config`` vocabulary. Every key and value is
validated before any DDL exists (fail closed): an unrecognized key is an
authoring error, never silently dropped. Sort keys are declared columns only,
no free-form expressions, so nothing author-written reaches DDL unquoted.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from continuo_engine_contract.config import ensure_known_keys  # type: ignore[import-untyped]

from .columns import is_temporal_type
from .identifiers import Identifier

ENGINE = "duckdb"
KNOWN_CONFIG_KEYS: tuple[str, ...] = ("partitioned_by", "sorted_by")
_PARTITION_ENTRY_KEYS: tuple[str, ...] = ("column", "transform", "buckets")
_SORT_ENTRY_KEYS: tuple[str, ...] = ("column", "direction", "nulls")
_TRANSFORMS: tuple[str, ...] = ("identity", "bucket", "year", "month", "day", "hour")
_TEMPORAL_TRANSFORMS = frozenset({"year", "month", "day", "hour"})
_DIRECTIONS: tuple[str, ...] = ("asc", "desc")
_NULL_ORDERS: tuple[str, ...] = ("first", "last")


@dataclass(frozen=True)
class PartitionKey:
    column: Identifier
    transform: str = "identity"
    buckets: int | None = None


@dataclass(frozen=True)
class SortKey:
    column: Identifier
    descending: bool = False
    nulls_first: bool | None = None  # None: the engine's default null ordering


@dataclass(frozen=True)
class TableLayout:
    partition_keys: tuple[PartitionKey, ...] = ()
    sort_keys: tuple[SortKey, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.partition_keys and not self.sort_keys

    @classmethod
    def empty(cls) -> "TableLayout":
        return cls()

    @classmethod
    def from_config(
        cls, config: Mapping[str, Any] | None, column_types: Mapping[str, str | None]
    ) -> "TableLayout":
        """Validate *config* and build the layout.

        *column_types* maps each declared column to its contract type, or to
        ``None`` when only the names are known (the harness's early hook): the
        time-transform type check is then deferred to ``ensure_table``.
        """
        if config is None:
            return cls.empty()
        ensure_known_keys(config, KNOWN_CONFIG_KEYS, ENGINE)
        partition_keys: tuple[PartitionKey, ...] = ()
        sort_keys: tuple[SortKey, ...] = ()
        if "partitioned_by" in config:
            partition_keys = _partition_keys(config["partitioned_by"], column_types)
        if "sorted_by" in config:
            sort_keys = _sort_keys(config["sorted_by"], column_types)
        return cls(partition_keys, sort_keys)


def _declared_column(value: Any, column_types: Mapping[str, str | None], where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where} 'column' must be a non-empty column name, got {value!r}")
    if value not in column_types:
        raise ValueError(
            f"{where} names undeclared column {value!r}; declared columns: "
            f"{sorted(column_types)!r}"
        )
    return value


def _partition_keys(raw: Any, column_types: Mapping[str, str | None]) -> tuple[PartitionKey, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"config 'partitioned_by' must be a non-empty list, got {raw!r}")
    keys: list[PartitionKey] = []
    for entry in raw:
        key = _partition_key(entry, column_types)
        if key in keys:
            raise ValueError(f"duplicate partition key: {entry!r}")
        keys.append(key)
    return tuple(keys)


def _partition_key(entry: Any, column_types: Mapping[str, str | None]) -> PartitionKey:
    if isinstance(entry, str):
        entry = {"column": entry}
    where = "config 'partitioned_by' entry"
    ensure_known_keys(entry, _PARTITION_ENTRY_KEYS, ENGINE, where=where)
    column = _declared_column(entry.get("column"), column_types, where)
    transform = entry.get("transform", "identity")
    if transform not in _TRANSFORMS:
        raise ValueError(
            f"unsupported partition 'transform' {transform!r}; supported: {', '.join(_TRANSFORMS)}"
        )
    buckets = entry.get("buckets")
    if transform == "bucket":
        if isinstance(buckets, bool) or not isinstance(buckets, int) or buckets < 1:
            raise ValueError(
                f"partition transform 'bucket' requires 'buckets' as a positive integer, got {buckets!r}"
            )
    elif buckets is not None:
        raise ValueError("'buckets' is only valid with partition transform 'bucket'")
    if transform in _TEMPORAL_TRANSFORMS:
        column_type = column_types[column]
        if column_type is not None and not is_temporal_type(column_type):
            raise ValueError(
                f"partition transform {transform!r} needs a DATE or TIMESTAMP column, "
                f"but {column!r} is {column_type}"
            )
    return PartitionKey(Identifier(column), transform, buckets)


def _sort_keys(raw: Any, column_types: Mapping[str, str | None]) -> tuple[SortKey, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"config 'sorted_by' must be a non-empty list, got {raw!r}")
    keys: list[SortKey] = []
    seen: set[str] = set()
    for entry in raw:
        if isinstance(entry, str):
            entry = {"column": entry}
        where = "config 'sorted_by' entry"
        ensure_known_keys(entry, _SORT_ENTRY_KEYS, ENGINE, where=where)
        column = _declared_column(entry.get("column"), column_types, where)
        if column in seen:
            raise ValueError(f"duplicate sort column: {column!r}")
        seen.add(column)
        direction = _choice(entry.get("direction", "asc"), _DIRECTIONS, "direction")
        nulls = entry.get("nulls")
        nulls_first = None if nulls is None else _choice(nulls, _NULL_ORDERS, "nulls") == "first"
        keys.append(SortKey(Identifier(column), direction == "desc", nulls_first))
    return tuple(keys)


def _choice(value: Any, allowed: tuple[str, ...], key: str) -> str:
    if not isinstance(value, str) or value.lower() not in allowed:
        raise ValueError(f"sort '{key}' must be one of {', '.join(allowed)}, got {value!r}")
    return value.lower()
