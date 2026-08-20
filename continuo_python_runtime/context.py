"""Runtime context for executing node reads."""

import types
from collections.abc import Callable
from typing import TYPE_CHECKING

import pyarrow as pa  # type: ignore[import-untyped]

from continuo_python_runtime.contract.model import Node
from continuo_python_runtime.errors import ReadError

if TYPE_CHECKING:
    from continuo_engine_contract.port import RuntimeAdapter  # type: ignore[import-untyped]


class RunContext:
    """Manages declared read access for a node execution."""

    def __init__(self, node: Node, adapter: "RuntimeAdapter") -> None:
        """Initialize context with a node and adapter.

        Args:
            node: The node definition with declared reads.
            adapter: Duck-typed adapter with .fetch(sql) method.
        """
        self._fetch: Callable[[str], pa.Table] = adapter.fetch
        self._reads: types.MappingProxyType[str, str] = types.MappingProxyType(
            dict(node.reads)
        )
        self._memo: dict[str, pa.Table] = {}

    def read(self, name: str) -> pa.Table:
        """Fetch a declared read by name, memoized.

        Args:
            name: The name of the declared read.

        Returns:
            A PyArrow table.

        Raises:
            ReadError: If name is not declared, or if adapter.fetch() fails.
        """
        if name not in self._reads:
            declared = sorted(self._reads.keys())
            raise ReadError(
                f"undeclared read {name!r}, declared: {declared}"
            )

        if name in self._memo:
            return self._memo[name]

        sql = self._reads[name]
        try:
            result = self._fetch(sql)
        except Exception as exc:
            raise ReadError(
                f"declared read {name!r} failed: {exc}"
            ) from exc

        self._memo[name] = result
        return result
