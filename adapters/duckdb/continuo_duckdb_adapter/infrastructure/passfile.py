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
import os
import tempfile


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
