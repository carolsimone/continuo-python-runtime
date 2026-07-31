"""Strict output-shape enforcer.

``conform()`` guards every python node's write: it enforces the declared
column set (extra/missing columns), reorders columns to the declared order,
performs a *strict* cast to the declared Arrow schema (raising rather than
silently truncating or corrupting data), enforces not-null constraints, and
checks VARCHAR/CHAR length limits.

``to_arrow()`` normalizes whatever a node's ``run()`` returned into a
``pyarrow.Table``.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.compute as pc  # type: ignore[import-untyped]

from continuo_python_runtime.contract.model import Column
from continuo_python_runtime.errors import ConformError, ScriptError
from continuo_python_runtime.types import arrow_type, parse_sql_type

logger = logging.getLogger("continuo_python_runtime.conform")


def to_arrow(obj: Any) -> pa.Table:
    """Normalize a node's ``run()`` return value into a ``pyarrow.Table``.

    Accepts, in order:
    - a ``pyarrow.Table`` (returned as-is, same object identity);
    - any object implementing the Arrow C stream protocol
      (``__arrow_c_stream__``), converted via ``pa.table(obj)``;
    - a pandas ``DataFrame`` (detected by duck-typing on module/class name,
      so pandas is never imported unless the object actually looks like a
      DataFrame), converted via ``pa.Table.from_pandas(obj,
      preserve_index=False)``.

    Anything else raises ``ScriptError``.

    Args:
        obj: The value returned by a node's ``run()`` function.

    Returns:
        A ``pyarrow.Table``.

    Raises:
        ScriptError: If ``obj`` is not Arrow-convertible.
    """
    if isinstance(obj, pa.Table):
        return obj

    if hasattr(obj, "__arrow_c_stream__"):
        return pa.table(obj)

    obj_type = type(obj)
    if obj_type.__module__.split(".")[0] == "pandas" and obj_type.__name__ == "DataFrame":
        try:
            import pandas  # type: ignore[import-untyped]  # noqa: F401
        except ImportError:
            pass
        else:
            return pa.Table.from_pandas(obj, preserve_index=False)

    raise ScriptError(
        f"run() returned {type(obj).__name__}; expected an Arrow-convertible value"
    )


def conform(
    table: pa.Table, columns: Sequence[Column], extra_columns: str = "raise"
) -> pa.Table:
    """Enforce the declared output shape on an Arrow table.

    Order of checks:
    0. Duplicate column names in the input table (always raises).
    1. Extra-column policy (raise or warn+drop).
    2. Missing columns (always raises).
    3. Select columns in declared order.
    4. Strict cast to the declared Arrow schema.
    5. Not-null enforcement.
    6. VARCHAR/CHAR length enforcement.

    Args:
        table: The Arrow table produced by a node's ``run()``.
        columns: The declared output columns, in declared order.
        extra_columns: ``"raise"`` (default) to fail on undeclared columns,
            or ``"warn"`` to drop them with a logged warning.

    Returns:
        A new ``pyarrow.Table`` matching the declared schema, column order,
        and constraints.

    Raises:
        ConformError: On any structural mismatch, strict-cast failure,
            null violation, or VARCHAR/CHAR overflow.
        ValueError: If ``extra_columns`` is not ``"raise"`` or ``"warn"``.
    """
    if extra_columns not in ("raise", "warn"):
        raise ValueError(f"extra_columns must be 'raise' or 'warn', got {extra_columns!r}")

    declared = [c.name for c in columns]

    counts = Counter(table.column_names)
    dups = sorted(name for name, count in counts.items() if count > 1)
    if dups:
        raise ConformError(f"dataframe has duplicate column(s): {dups}")

    extra = [n for n in table.column_names if n not in declared]
    if extra:
        if extra_columns == "raise":
            raise ConformError(
                f"dataframe has undeclared column(s): {extra}; declared: {declared}"
            )
        logger.warning("conform: dropping undeclared column(s) %s", extra)

    missing = [n for n in declared if n not in table.column_names]
    if missing:
        raise ConformError(f"dataframe is missing column(s): {missing}")

    table = table.select(declared)

    # The cast target schema is always nullable here: nullability is a
    # *value* constraint (checked below against actual data), not something
    # we want pyarrow's cast to enforce structurally — a non-nullable target
    # field makes `cast()` raise ValueError on any null, before we get a
    # chance to report it as a ConformError with a useful message.
    target = pa.schema(
        [pa.field(c.name, arrow_type(parse_sql_type(c.type)), nullable=True) for c in columns]
    )
    try:
        table = table.cast(target, safe=True)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError, ValueError) as exc:
        raise ConformError(f"strict cast to declared schema failed: {exc}") from exc

    for col in columns:
        if not col.nullable and table.column(col.name).null_count:
            raise ConformError(
                f"column {col.name} is declared nullable: false but contains nulls"
            )
        sql_t = parse_sql_type(col.type)
        if sql_t.length is not None:
            max_len = pc.max(pc.utf8_length(table.column(col.name))).as_py()
            if max_len is not None and max_len > sql_t.length:
                raise ConformError(
                    f"column {col.name}: value length {max_len} exceeds "
                    f"{sql_t.base}({sql_t.length})"
                )

    return table
