"""The primary-key invariant every upsert relies on, checked once for every engine.

An upsert whose own rows repeat a key cannot be written consistently: DuckLake's
MERGE inserts both rows of a repeated new key and keeps the last row of a
repeated existing one, Trino fails only when two rows match one existing row,
and postgres's ON CONFLICT fails within one statement but keeps the last row
across statements. ``UpsertCapable.upsert`` therefore calls
:func:`ensure_unique_key` before any engine sees the rows.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    import pyarrow  # type: ignore[import-untyped]

# How many repeated keys the error message names before summarizing the rest.
_SHOWN_KEYS = 5


class DuplicateKeyError(ValueError):
    """The rows to upsert repeat a primary key; nothing was written."""


def ensure_unique_key(data: "pyarrow.Table", primary_key: Sequence[str]) -> None:
    """Raise :class:`DuplicateKeyError` if two rows of *data* share a *primary_key* value.

    Groups the rows by the key columns. NULL counts as a value here; the
    contract requires every key column to be ``nullable: false`` and the
    harness's ``conform()`` rejects NULLs in such columns before this runs.
    The message names the first repeated keys, in order of first appearance,
    with their row counts. Only methods of *data* are called, so this module
    needs no pyarrow import.

    Raises:
        ValueError: If *primary_key* is empty or names a column *data* lacks.
        DuplicateKeyError: If a key value appears in more than one row.
    """
    key = list(primary_key)
    if not key:
        raise ValueError("primary_key must name at least one column")
    missing = [name for name in key if name not in data.column_names]
    if missing:
        raise ValueError(
            f"primary key column(s) {missing} are not in the data's columns {data.column_names}"
        )
    if data.num_rows < 2:
        return
    # Positional names: a key column called "count_all" must not collide with
    # the aggregate's own output column.
    positional = [f"k{index}" for index in range(len(key))]
    counts = (
        data.select(key)
        .rename_columns(positional)
        .group_by(positional, use_threads=False)
        .aggregate([([], "count_all")])
    )
    if counts.num_rows == data.num_rows:
        return
    repeated = [row for row in counts.to_pylist() if row["count_all"] > 1]
    shown = "; ".join(
        f"({', '.join(str(row[name]) for name in positional)}) appears {row['count_all']} times"
        for row in repeated[:_SHOWN_KEYS]
    )
    more = (
        f"; and {len(repeated) - _SHOWN_KEYS} more repeated key(s)"
        if len(repeated) > _SHOWN_KEYS
        else ""
    )
    raise DuplicateKeyError(
        f"primary key ({', '.join(key)}) is not unique in the rows to write: "
        f"{shown}{more}; nothing was written"
    )
