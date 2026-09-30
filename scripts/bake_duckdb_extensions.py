"""Install the DuckDB extensions the duckdb adapter needs into the image.

Run once at image build time, as root, into DUCKDB_EXTENSION_DIRECTORY. The
runtime container is non-root and must start without network access, so the
adapter only LOADs these, it never has to INSTALL them.
"""
import logging
import os
import sys

import duckdb

logger = logging.getLogger("bake_duckdb_extensions")

EXTENSIONS = ("ducklake", "postgres", "httpfs")


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    directory = os.environ["DUCKDB_EXTENSION_DIRECTORY"]
    os.makedirs(directory, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET extension_directory = '" + directory.replace("'", "''") + "'")
    for name in EXTENSIONS:
        logger.info("installing %s into %s", name, directory)
        con.execute(f"INSTALL {name}")
        con.execute(f"LOAD {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
