"""A private libpq passfile that keeps the catalog password out of the DSN.

DuckDB echoes the ATTACH conninfo in its own error strings and exposes it in
``duckdb_databases().path``, so a ``password=`` keyword would leak there. A
``passfile=`` keyword points libpq at a mode-0600 file instead. Unlike the
``PGPASSWORD`` environment variable this is scoped to the one connection: it
cannot silently authenticate unrelated postgres connections made elsewhere in
the same process (for example by a node script).
"""
from __future__ import annotations

import atexit
import contextlib
import logging
import os
import tempfile

logger = logging.getLogger("continuo_duckdb_adapter")


def _escape(field: str) -> str:
    return field.replace("\\", "\\\\").replace(":", "\\:")


def can_store(password: str) -> bool:
    """A passfile is line-oriented: it cannot carry a newline in the password."""
    return bool(password) and "\n" not in password and "\r" not in password


class Passfile:
    """One temporary ``*:*:*:*:<password>`` file, created 0600, removed on close."""

    def __init__(self, password: str) -> None:
        fd, self.path = tempfile.mkstemp(prefix="cpr-pgpass-")  # mode 0600
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(f"*:*:*:*:{_escape(password)}\n")
        except BaseException:
            self.close()
            raise
        # A session that is never closed must not leave the password on disk.
        atexit.register(self.close)

    def close(self) -> None:
        atexit.unregister(self.close)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)


def open_passfile(password: str) -> Passfile | None:
    """A passfile for *password*, or None when the password must go inline.

    Inline is the fallback when the password cannot be stored (empty, or with a
    line break), when ``PGPASSWORD`` is set (libpq fills the password from it
    before it reads any passfile, so the passfile would be ignored), or when no
    temp file can be created (for example a read-only root filesystem).
    """
    if not can_store(password):
        return None
    if "PGPASSWORD" in os.environ:
        logger.info("PGPASSWORD is set; passing the catalog password inline instead of a passfile")
        return None
    try:
        return Passfile(password)
    except OSError as exc:
        logger.info("could not create a passfile (%s); passing the catalog password inline", type(exc).__name__)
        return None
