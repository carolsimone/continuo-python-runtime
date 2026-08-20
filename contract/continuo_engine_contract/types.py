"""The contract's SQL column-type grammar.

The matched text is interpolated directly into engine DDL by the adapters, so
this is an injection guard, not just validation — anything outside this shape
is rejected outright. Kept byte-identical to the runtime adapters' grammar in
continuo-python-runtime so both planes accept the same type set.
"""
import re

TYPE_RE = re.compile(
    r"^("
    r"BIGINT|INT|INTEGER|DOUBLE PRECISION|TEXT|TIMESTAMP|DATE|BOOLEAN|"
    r"(NUMERIC|DECIMAL)\(\d+,\s*\d+\)|"
    r"(VARCHAR|CHAR)\(\d+\)"
    r")\Z",
    re.IGNORECASE | re.ASCII,
)


def validate_column_type(type_str: str) -> None:
    """Raise ValueError unless *type_str* matches the contract's SQL type grammar."""
    if not TYPE_RE.match(type_str):
        raise ValueError(f"unsupported or invalid SQL type: {type_str!r}")
