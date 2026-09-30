"""Render domain objects into DuckDB/DuckLake SQL.

Every identifier goes through ``quote_identifier`` and every string value through
``sql_literal``; types, transforms, directions and null orders come from
validated domain values, never raw input. Own DDL is fully catalog-qualified
(``"lake"."schema"."table"``) so a schema named like an attached database cannot
be mis-resolved.
"""
from __future__ import annotations

import re
from collections.abc import Sequence

from ..domain.columns import ColumnDefinition
from ..domain.identifiers import Identifier, QualifiedTable
from ..domain.layout import PartitionKey, SortKey
from .settings import DuckLakeSettings

_LIBPQ_BARE = re.compile(r"[A-Za-z0-9_.:/\-]+")

# The one list of DuckDB extensions the adapter needs: the session LOADs them,
# the image bake script INSTALLs them, the offline image check LOADs them.
EXTENSIONS: tuple[str, ...] = ("ducklake", "postgres", "httpfs")


BEGIN = "BEGIN"
COMMIT = "COMMIT"
ROLLBACK = "ROLLBACK"


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def load_extension(name: str) -> str:
    return f"LOAD {quote_identifier(name)}"


def install_extension(name: str) -> str:
    return f"INSTALL {quote_identifier(name)}"


def set_extension_directory(directory: str) -> str:
    return f"SET extension_directory = {sql_literal(directory)}"


def use_catalog(catalog: str) -> str:
    return f"USE {quote_identifier(catalog)}"


def _libpq_value(value: str) -> str:
    """Quote one libpq ``keyword=value`` value only when it needs it."""
    if _LIBPQ_BARE.fullmatch(value):
        return value
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def secret_texts(settings: DuckLakeSettings) -> tuple[str, ...]:
    """Every spelling of the credentials that can appear in an engine error message.

    The raw value, its libpq-quoted form (as written into the ATTACH conninfo)
    and the SQL-literal-doubled form of that, longest first so redaction of a
    longer spelling is not pre-empted by a shorter one.
    """
    texts: set[str] = set()
    for secret in (settings.catalog_password, settings.s3_secret_access_key):
        if not secret:
            continue
        quoted = _libpq_value(secret)
        texts.update((secret, quoted, quoted.replace("'", "''"), secret.replace("'", "''")))
    return tuple(sorted(texts, key=len, reverse=True))


def attach_statement(
    settings: DuckLakeSettings, catalog: str, passfile: str | None = None
) -> str:
    """The ATTACH for the DuckLake catalog.

    With *passfile* the conninfo names that libpq passfile and carries no
    password at all; without it the password is written inline.
    """
    credential = ("passfile", passfile) if passfile else ("password", settings.catalog_password)
    conninfo = " ".join(
        f"{key}={_libpq_value(value)}"
        for key, value in (
            ("host", settings.catalog_host),
            ("port", settings.catalog_port),
            ("dbname", settings.catalog_db),
            ("user", settings.catalog_user),
            credential,
        )
    )
    options = [f"DATA_PATH {sql_literal(settings.data_path)}"]
    if settings.data_inlining_row_limit is not None:
        options.append(f"DATA_INLINING_ROW_LIMIT {settings.data_inlining_row_limit}")
    return (
        f"ATTACH {sql_literal('ducklake:postgres:' + conninfo)} "
        f"AS {quote_identifier(catalog)} ({', '.join(options)})"
    )


def s3_secret_statement(settings: DuckLakeSettings) -> str:
    parts = ["TYPE S3"]
    if settings.s3_access_key_id and settings.s3_secret_access_key:
        parts.append(f"KEY_ID {sql_literal(settings.s3_access_key_id)}")
        parts.append(f"SECRET {sql_literal(settings.s3_secret_access_key)}")
    else:
        parts.append("PROVIDER credential_chain")
    if settings.s3_endpoint:
        parts.append(f"ENDPOINT {sql_literal(settings.s3_endpoint)}")
    parts.append(f"REGION {sql_literal(settings.s3_region)}")
    parts.append(f"URL_STYLE {sql_literal(settings.s3_url_style)}")
    parts.append(f"USE_SSL {'true' if settings.s3_use_ssl else 'false'}")
    return f"CREATE OR REPLACE SECRET continuo_s3 ({', '.join(parts)})"


class DdlRenderer:
    """SQL text for every LakeGateway operation, against one catalog alias."""

    TABLE_EXISTS_QUERY = (
        "SELECT count(*) FROM duckdb_tables() "
        "WHERE database_name = ? AND schema_name = ? AND table_name = ?"
    )

    def __init__(self, catalog: str) -> None:
        self._catalog = catalog

    @property
    def catalog_name(self) -> str:
        return self._catalog

    def schema_ref(self, schema: Identifier) -> str:
        return f"{quote_identifier(self._catalog)}.{quote_identifier(schema.name)}"

    def table_ref(self, table: QualifiedTable) -> str:
        return f"{self.schema_ref(table.schema)}.{quote_identifier(table.table.name)}"

    def create_schema_if_not_exists(self, schema: Identifier) -> str:
        return f"CREATE SCHEMA IF NOT EXISTS {self.schema_ref(schema)}"

    def drop_schema_cascade(self, schema: Identifier) -> str:
        return f"DROP SCHEMA IF EXISTS {self.schema_ref(schema)} CASCADE"

    def drop_table_if_exists(self, table: QualifiedTable) -> str:
        return f"DROP TABLE IF EXISTS {self.table_ref(table)}"

    def create_table(
        self,
        table: QualifiedTable,
        columns: Sequence[ColumnDefinition],
        *,
        if_not_exists: bool = False,
    ) -> str:
        defs = ", ".join(
            f"{quote_identifier(c.name.name)} {c.type}" + ("" if c.nullable else " NOT NULL")
            for c in columns
        )
        clause = " IF NOT EXISTS" if if_not_exists else ""
        return f"CREATE TABLE{clause} {self.table_ref(table)} ({defs})"

    def create_empty_table_as(self, table: QualifiedTable, select_sql: str) -> str:
        # The newline before ")" keeps a trailing "-- comment" in the read from
        # swallowing the closing parenthesis.
        return f"CREATE TABLE {self.table_ref(table)} AS (\n{select_sql}\n) WITH NO DATA"

    def create_empty_clone(self, target: QualifiedTable, source: QualifiedTable) -> str:
        return (
            f"CREATE TABLE {self.table_ref(target)} AS "
            f"SELECT * FROM {self.table_ref(source)} WHERE false"
        )

    def set_partitioned_by(self, table: QualifiedTable, keys: Sequence[PartitionKey]) -> str:
        return f"ALTER TABLE {self.table_ref(table)} SET PARTITIONED BY ({', '.join(map(self._partition, keys))})"

    def set_sorted_by(self, table: QualifiedTable, keys: Sequence[SortKey]) -> str:
        return f"ALTER TABLE {self.table_ref(table)} SET SORTED BY ({', '.join(map(self._sort, keys))})"

    def explain_read(self, sql: str) -> str:
        return f"EXPLAIN SELECT * FROM (\n{sql}\n) AS __check_binds__"

    def delete_all(self, table: QualifiedTable) -> str:
        return f"DELETE FROM {self.table_ref(table)}"

    def insert_select(self, table: QualifiedTable, column_names: Sequence[str], source: str) -> str:
        cols = ", ".join(quote_identifier(name) for name in column_names)
        return f"INSERT INTO {self.table_ref(table)} ({cols}) SELECT {cols} FROM {quote_identifier(source)}"

    @staticmethod
    def _partition(key: PartitionKey) -> str:
        column = quote_identifier(key.column.name)
        if key.transform == "identity":
            return column
        if key.transform == "bucket":
            return f"bucket({key.buckets}, {column})"
        return f"{key.transform}({column})"

    @staticmethod
    def _sort(key: SortKey) -> str:
        text = f"{quote_identifier(key.column.name)} {'DESC' if key.descending else 'ASC'}"
        if key.nulls_first is not None:
            text += " NULLS FIRST" if key.nulls_first else " NULLS LAST"
        return text
