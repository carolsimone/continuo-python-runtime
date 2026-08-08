# Base image tag published by this repo's image pipeline (PR 9). Tags are
# v<version>-<engine>, e.g. v0.2.0-postgres; a trino-backed variant is also
# published as v0.2.0-trino for trino-backed services.
FROM ghcr.io/carolsimone/continuo-python-runtime:v0.2.0-postgres
COPY contracts/ /app/contracts/
COPY scripts/ /app/scripts/
# COPY every OTHER directory your scripts import from, too. CI folds a script's
# transitive in-repo import closure into shared_code_hash, so a helper module in
# e.g. lib/ or common/ is hashed whether or not it is in the image — a helper
# left out here produces a valid release artifact and a node that dies with
# ModuleNotFoundError on its first run. Add one line per such directory, e.g.:
# COPY lib/ /app/lib/
# Add your dataframe libraries here, e.g.:
# RUN pip install --no-cache-dir polars==1.34.0
