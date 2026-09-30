"""The DuckDB extensions the adapter needs, and the offline check for them.

The session LOADs them, the image bake script INSTALLs them and the offline
image check LOADs them, so the list and the check live in this one module. The
SQL statements themselves are rendered by ``ddl``.
"""
from __future__ import annotations

import logging

import duckdb

from .ddl import (
    disable_extension_autoinstall,
    load_extension,
    set_extension_directory,
)

logger = logging.getLogger("continuo_duckdb_adapter")

# ``aws`` backs ``CREATE SECRET ... PROVIDER credential_chain``.
EXTENSIONS: tuple[str, ...] = ("ducklake", "postgres", "httpfs", "aws")


def check_offline(directory: str) -> None:
    """LOAD every extension from *directory* without ever installing one.

    What a baked, offline, non-root image must be able to do; raises
    ``duckdb.Error`` naming the first extension that is missing. Autoinstall is
    disabled first, otherwise a missing extension would be silently downloaded.
    """
    con = duckdb.connect()
    try:
        con.execute(set_extension_directory(directory))
        con.execute(disable_extension_autoinstall())
        for name in EXTENSIONS:
            con.execute(load_extension(name))
            logger.info("loaded %s offline from %s", name, directory)
    finally:
        con.close()
