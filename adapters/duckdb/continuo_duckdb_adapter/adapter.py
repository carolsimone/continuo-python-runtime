"""Composition root: wires settings, session and use cases. Entry-point target.

The only module that knows every layer. The use cases live in
``application.warehouse.LakeWarehouse``; this class supplies the two hooks the
port leaves to the concrete engine, ``required_env`` and ``from_env``.
"""
from __future__ import annotations

from .application.warehouse import LakeWarehouse
from .infrastructure.session import DuckLakeSession
from .infrastructure.settings import REQUIRED_ENV, DuckLakeSettings


class DuckDBAdapter(LakeWarehouse):
    """WarehouseAdapter for DuckDB on a DuckLake (Postgres catalog, S3 data)."""

    @classmethod
    def required_env(cls) -> list[str]:
        """Vars that must be non-empty before connecting."""
        return list(REQUIRED_ENV)

    @classmethod
    def from_env(cls) -> "DuckDBAdapter":
        """Connect from DUCKDB_* env (see DuckLakeSettings for every variable)."""
        return cls(DuckLakeSession.connect(DuckLakeSettings.from_env()))
