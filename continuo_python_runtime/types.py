"""SQL type parsing and Arrow type mapping.

Parses SQL type strings into a canonical representation and provides mapping
to PyArrow types for schema generation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pyarrow as pa  # type: ignore[import-untyped]
from continuo_engine_contract.types import validate_column_type  # type: ignore[import-untyped]

from continuo_python_runtime.errors import ContractError


@dataclass(frozen=True)
class SqlType:
    """Canonical SQL type representation.

    Attributes:
        base: Canonicalized base type name (one of BIGINT, INTEGER,
            DOUBLE_PRECISION, NUMERIC, VARCHAR, CHAR, TEXT, TIMESTAMP,
            DATE, BOOLEAN).
        precision: For NUMERIC types, the total number of digits.
        scale: For NUMERIC types, the number of digits after the decimal.
        length: For VARCHAR and CHAR types, the maximum length in characters.
    """

    base: str
    precision: int | None = None
    scale: int | None = None
    length: int | None = None


def parse_sql_type(raw: str) -> SqlType:
    """Parse a SQL type string into a canonical SqlType.

    ``continuo_engine_contract.types.validate_column_type`` is the single
    acceptance authority for the grammar shape (case-insensitive, injection-
    guarded): it runs first, and anything it rejects is rejected here too. The
    logic below only extracts precision/scale/length and enforces the NUMERIC
    range this repo's Arrow mapping additionally requires (1-38 precision,
    0-precision scale) -- a semantic check the shared grammar deliberately
    doesn't make, since it's specific to this repo's decimal128 mapping.

    Args:
        raw: A SQL type string (case-insensitive).

    Returns:
        A SqlType with canonicalized base and parsed parameters.

    Raises:
        ContractError: If the type is unsupported or malformed, or a NUMERIC's
            precision/scale is out of range.
    """
    raw = raw.strip()
    try:
        validate_column_type(raw)
    except ValueError as exc:
        raise ContractError(str(exc)) from exc

    # Normalize to uppercase for parsing
    normalized = raw.upper()

    # Handle DOUBLE PRECISION specially (has a space). It is the only grammar
    # member whose canonical SqlType.base spelling (DOUBLE_PRECISION, with an
    # underscore) differs from its authored one.
    if normalized == "DOUBLE PRECISION":
        return SqlType("DOUBLE_PRECISION")

    # Parametrized types: TYPE(args). validate_column_type above has already
    # rejected every shape but (NUMERIC|DECIMAL)(\d+,\s*\d+) and
    # (VARCHAR|CHAR)(\d+), so no further shape checking is needed here.
    match = re.match(r"^([A-Z_]+)\s*\((.*)\)$", normalized)
    if match:
        base_name = match.group(1).strip()
        params_str = match.group(2).strip()

        if base_name == "DECIMAL":
            base_name = "NUMERIC"

        if base_name == "NUMERIC":
            precision_str, scale_str = params_str.split(",")
            precision = int(precision_str)
            scale = int(scale_str)
            if not (1 <= precision <= 38):
                raise ContractError(
                    f"type: NUMERIC precision must be between 1 and 38, got {precision}"
                )
            if not (0 <= scale <= precision):
                raise ContractError(
                    f"type: NUMERIC scale must be between 0 and precision ({precision}), "
                    f"got {scale}"
                )
            return SqlType("NUMERIC", precision=precision, scale=scale)

        # VARCHAR(length) or CHAR(length)
        return SqlType(base_name, length=int(params_str))

    # Bare base type: BIGINT, INT, INTEGER, TEXT, TIMESTAMP, DATE, BOOLEAN --
    # the only shapes left once DOUBLE PRECISION and the parametrized types
    # above are ruled out.
    normalized_base = "INTEGER" if normalized == "INT" else normalized
    return SqlType(normalized_base)


def arrow_type(t: SqlType) -> pa.DataType:
    """Map a SqlType to a PyArrow DataType.

    Args:
        t: A SqlType instance.

    Returns:
        A PyArrow DataType corresponding to the SQL type.

    Raises:
        ValueError: If the SqlType is invalid or incomplete.
    """
    if t.base == "BIGINT":
        return pa.int64()
    elif t.base == "INTEGER":
        return pa.int32()
    elif t.base == "DOUBLE_PRECISION":
        return pa.float64()
    elif t.base == "NUMERIC":
        if t.precision is None or t.scale is None:
            raise ValueError(f"NUMERIC requires precision and scale, got {t}")
        return pa.decimal128(t.precision, t.scale)
    elif t.base == "VARCHAR":
        return pa.string()
    elif t.base == "CHAR":
        return pa.string()
    elif t.base == "TEXT":
        return pa.string()
    elif t.base == "TIMESTAMP":
        return pa.timestamp("us")
    elif t.base == "DATE":
        return pa.date32()
    elif t.base == "BOOLEAN":
        return pa.bool_()
    else:
        raise ValueError(f"unknown SQL type base: {t.base}")
