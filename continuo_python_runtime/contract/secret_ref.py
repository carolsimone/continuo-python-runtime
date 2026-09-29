"""The name of the Kubernetes Secret a node's pod receives as env vars.

The ``continuo-api-`` prefix keeps a contract from naming any other Secret in
the namespace (the warehouse credentials, the platform's own credentials).
continuo's topology-controller and execution-controller enforce the same
pattern; this check gives the author the error in CI instead.
"""

from __future__ import annotations

import re

from continuo_python_runtime.errors import ContractError

SECRET_REF_PATTERN = re.compile(r"^continuo-api-[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
SECRET_REF_MAX_LEN = 253


def validate_secret_ref(value: object, label: str) -> str:
    """Return ``value`` if it is a valid ``continuo-api-*`` Secret name."""
    if (
        not isinstance(value, str)
        or len(value) > SECRET_REF_MAX_LEN
        or not SECRET_REF_PATTERN.fullmatch(value)
    ):
        raise ContractError(
            f"{label}: 'secret_ref' must be a Kubernetes Secret name starting with "
            f"'continuo-api-' (lowercase letters, digits, '-'; at most "
            f"{SECRET_REF_MAX_LEN} chars), got {value!r}"
        )
    return value
