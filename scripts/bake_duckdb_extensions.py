"""Install the DuckDB extensions the duckdb adapter needs into the image.

Run once at image build time, as root, into DUCKDB_EXTENSION_DIRECTORY. The
runtime container is non-root and must start without network access, so the
adapter only LOADs these, it never has to INSTALL them. The extension list and
the statements come from the adapter package, so they cannot drift from it.
"""
import logging
import os
import sys

import duckdb
from continuo_duckdb_adapter.infrastructure.ddl import (
    EXTENSIONS,
    install_extension,
    load_extension,
    set_extension_directory,
)

logger = logging.getLogger("bake_duckdb_extensions")


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    directory = os.environ["DUCKDB_EXTENSION_DIRECTORY"]
    os.makedirs(directory, exist_ok=True)
    con = duckdb.connect()
    con.execute(set_extension_directory(directory))
    for name in EXTENSIONS:
        logger.info("installing %s into %s", name, directory)
        con.execute(install_extension(name))
        con.execute(load_extension(name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
