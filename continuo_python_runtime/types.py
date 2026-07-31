"""SQL type parsing and Arrow type mapping.

Parses SQL type strings into a canonical representation and provides mapping
to PyArrow types for schema generation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pyarrow as pa  # type: ignore[import-untyped]

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

    Args:
        raw: A SQL type string (case-insensitive).

    Returns:
        A SqlType with canonicalized base and parsed parameters.

    Raises:
        ContractError: If the type is unsupported or malformed.
    """
    raw = raw.strip()
    if not raw:
        raise ContractError("type: empty string is not a valid SQL type")

    # Normalize to uppercase for parsing
    normalized = raw.upper()

    # Handle DOUBLE PRECISION specially (has a space)
    if normalized == "DOUBLE PRECISION":
        return SqlType("DOUBLE_PRECISION")

    # Try to match parametrized types: TYPE(args)
    match = re.match(r"^([A-Z_]+)\s*\((.*)\)$", normalized)
    if match:
        base_name = match.group(1).strip()
        params_str = match.group(2).strip()

        # Alias handling
        if base_name == "INT":
            base_name = "INTEGER"
        elif base_name == "DECIMAL":
            base_name = "NUMERIC"

        if base_name == "NUMERIC":
            # Parse NUMERIC(precision, scale)
            params = [p.strip() for p in params_str.split(",")]
            if len(params) != 2:
                raise ContractError(
                    f"type: NUMERIC requires exactly 2 parameters (precision, scale), "
                    f"got {len(params)}"
                )
            try:
                precision = int(params[0])
                scale = int(params[1])
            except ValueError as e:
                raise ContractError(
                    f"type: NUMERIC parameters must be integers, got ({params[0]}, {params[1]})"
                ) from e
            return SqlType("NUMERIC", precision=precision, scale=scale)

        elif base_name == "VARCHAR":
            # Parse VARCHAR(length)
            if "," in params_str:
                raise ContractError(
                    f"type: VARCHAR takes a single parameter (length), "
                    f"got {params_str!r}"
                )
            try:
                length = int(params_str)
            except ValueError as e:
                raise ContractError(
                    f"type: VARCHAR length must be an integer, got {params_str!r}"
                ) from e
            return SqlType("VARCHAR", length=length)

        elif base_name == "CHAR":
            # Parse CHAR(length)
            if "," in params_str:
                raise ContractError(
                    f"type: CHAR takes a single parameter (length), "
                    f"got {params_str!r}"
                )
            try:
                length = int(params_str)
            except ValueError as e:
                raise ContractError(
                    f"type: CHAR length must be an integer, got {params_str!r}"
                ) from e
            return SqlType("CHAR", length=length)

        else:
            # No other types accept parameters
            raise ContractError(
                f"type: {base_name} does not accept parameters, got ({params_str})"
            )

    # No parameters - must be a bare base type
    normalized_base = normalized

    # Alias handling
    if normalized_base == "INT":
        normalized_base = "INTEGER"
    elif normalized_base == "DECIMAL":
        normalized_base = "NUMERIC"

    # List of supported bare types
    supported_bare = {
        "BIGINT",
        "INTEGER",
        "DOUBLE_PRECISION",
        "TEXT",
        "TIMESTAMP",
        "DATE",
        "BOOLEAN",
    }

    if normalized_base in supported_bare:
        return SqlType(normalized_base)

    # Bare VARCHAR and CHAR require a length parameter
    if normalized_base == "VARCHAR":
        raise ContractError(f"type: VARCHAR requires a length parameter, got {raw!r}")
    if normalized_base == "CHAR":
        raise ContractError(f"type: CHAR requires a length parameter, got {raw!r}")

    # Bare NUMERIC and DECIMAL require parameters
    if normalized_base == "NUMERIC":
        raise ContractError(f"type: NUMERIC requires precision and scale parameters, got {raw!r}")

    # Unknown type
    raise ContractError(f"type: unknown SQL type {raw!r}")


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
