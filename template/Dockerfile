# Base image tag published by this repo's image pipeline (PR 9). Tags are
# v<version>-<engine>, e.g. v0.1.0-postgres; a trino-backed variant is also
# published as v0.1.0-trino for trino-backed services.
FROM ghcr.io/carolsimone/continuo-python-runtime:v0.1.0-postgres
COPY contracts/ /app/contracts/
COPY scripts/ /app/scripts/
# Add your dataframe libraries here, e.g.:
# RUN pip install --no-cache-dir polars==1.34.0
