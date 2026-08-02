# Base image tag published by this repo's image pipeline (PR 9)
FROM ghcr.io/carolsimone/continuo-python-runtime:0.1.0-postgres
COPY contracts/ /app/contracts/
COPY scripts/ /app/scripts/
# Add your dataframe libraries here, e.g.:
# RUN pip install --no-cache-dir polars==1.34.0
