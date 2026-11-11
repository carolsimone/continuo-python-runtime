# Tags are v<version>; one image per engine (continuo-python-runtime-postgres / -trino).
FROM ghcr.io/carolsimone/continuo-python-runtime-postgres:v0.11.0
COPY contracts/ /app/contracts/
COPY scripts/ /app/scripts/
# COPY every OTHER directory your scripts import from or read, too. CI folds a
# script's transitive in-repo import closure and its declared `assets` into
# shared_code_hash, so a helper module in e.g. lib/ or common/, or a data file in
# e.g. data/, is hashed whether or not it is in the image — one left out here
# produces a valid release artifact and a node that dies with ModuleNotFoundError
# or FileNotFoundError on its first run. Add one line per such directory, e.g.:
# COPY lib/ /app/lib/
# Add your dataframe libraries here. This image's base runs as non-root
# (uid 65532), which pip cannot install under, so switch to root for the
# install and switch back — the executor's pod spec expects uid 65532:
# USER root
# RUN pip install --no-cache-dir polars==1.34.0
# USER 65532:65532
