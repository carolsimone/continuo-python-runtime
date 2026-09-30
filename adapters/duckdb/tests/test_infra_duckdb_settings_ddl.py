"""Settings parsing and SQL rendering: pure string work, no engine."""
import duckdb
import pytest

from continuo_duckdb_adapter.domain.columns import ColumnDefinition
from continuo_duckdb_adapter.domain.identifiers import Identifier, QualifiedTable
from continuo_duckdb_adapter.domain.layout import PartitionKey, SortKey
from continuo_duckdb_adapter.infrastructure.ddl import (
    EXTENSIONS, DdlRenderer, attach_statement, install_extension, load_extension,
    quote_identifier, s3_secret_statement, secret_texts, set_extension_directory, sql_literal,
    use_catalog,
)
from continuo_duckdb_adapter.infrastructure.session import check_offline
from continuo_duckdb_adapter.infrastructure.settings import REQUIRED_ENV, DuckLakeSettings

ENV = {
    "DUCKDB_CATALOG_HOST": "localhost", "DUCKDB_CATALOG_DB": "catalog",
    "DUCKDB_CATALOG_USER": "continuo", "DUCKDB_DATA_PATH": "s3://warehouse/lake/",
}


def test_required_env_lists_the_four_mandatory_vars():
    assert list(REQUIRED_ENV) == [
        "DUCKDB_CATALOG_HOST", "DUCKDB_CATALOG_DB", "DUCKDB_CATALOG_USER", "DUCKDB_DATA_PATH",
    ]


@pytest.mark.parametrize("missing", REQUIRED_ENV)
def test_missing_required_var_is_named(missing):
    env = {k: v for k, v in ENV.items() if k != missing}
    with pytest.raises(ValueError, match=missing):
        DuckLakeSettings.from_env(env)


def test_defaults():
    s = DuckLakeSettings.from_env(ENV)
    assert (s.catalog_port, s.catalog_password) == ("5432", "")
    assert (s.s3_region, s.s3_use_ssl, s.s3_url_style) == ("us-east-1", True, "vhost")
    assert s.s3_endpoint is None and s.s3_access_key_id is None
    assert s.extension_directory is None and s.data_inlining_row_limit is None
    assert s.uses_s3


def test_endpoint_defaults_to_path_style_and_overrides_apply():
    s = DuckLakeSettings.from_env({
        **ENV, "DUCKDB_S3_ENDPOINT": "localhost:19100", "DUCKDB_S3_USE_SSL": "false",
        "DUCKDB_S3_ACCESS_KEY_ID": "k", "DUCKDB_S3_SECRET_ACCESS_KEY": "s",
        "DUCKDB_EXTENSION_DIRECTORY": "/opt/x", "DUCKDB_DATA_INLINING_ROW_LIMIT": "0",
        "DUCKDB_CATALOG_PORT": "15599", "DUCKDB_CATALOG_PASSWORD": "pw",
    })
    assert (s.s3_url_style, s.s3_use_ssl, s.data_inlining_row_limit) == ("path", False, 0)
    assert (s.catalog_port, s.catalog_password, s.extension_directory) == ("15599", "pw", "/opt/x")


@pytest.mark.parametrize("name,value", [
    ("DUCKDB_DATA_INLINING_ROW_LIMIT", "many"), ("DUCKDB_S3_URL_STYLE", "sideways"),
    ("DUCKDB_CATALOG_PORT", "five"),
])
def test_invalid_values_name_the_variable(name, value):
    with pytest.raises(ValueError, match=name):
        DuckLakeSettings.from_env({**ENV, name: value})


def test_repr_hides_the_credentials():
    s = DuckLakeSettings.from_env({
        **ENV, "DUCKDB_CATALOG_PASSWORD": "pw-Hidden-1",
        "DUCKDB_S3_ACCESS_KEY_ID": "key-id", "DUCKDB_S3_SECRET_ACCESS_KEY": "s3-Hidden-2",
    })
    text = repr(s)
    assert "pw-Hidden-1" not in text and "s3-Hidden-2" not in text
    assert "catalog_host='localhost'" in text


@pytest.mark.parametrize("name", ["DUCKDB_CATALOG_PORT", "DUCKDB_DATA_INLINING_ROW_LIMIT"])
@pytest.mark.parametrize("value", ["²", "٣", "-1", "1.5"])
def test_numeric_vars_accept_only_ascii_digits(name, value):
    with pytest.raises(ValueError, match=name):
        DuckLakeSettings.from_env({**ENV, name: value})


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("Yes", True),
    ("false", False), ("False", False), ("0", False), ("no", False), ("NO", False),
])
def test_s3_use_ssl_accepts_boolean_words(value, expected):
    assert DuckLakeSettings.from_env({**ENV, "DUCKDB_S3_USE_SSL": value}).s3_use_ssl is expected


@pytest.mark.parametrize("value", ["flase", "on", "2", "enabled"])
def test_s3_use_ssl_rejects_anything_else(value):
    with pytest.raises(ValueError, match="DUCKDB_S3_USE_SSL"):
        DuckLakeSettings.from_env({**ENV, "DUCKDB_S3_USE_SSL": value})


@pytest.mark.parametrize("only", ["DUCKDB_S3_ACCESS_KEY_ID", "DUCKDB_S3_SECRET_ACCESS_KEY"])
def test_half_configured_static_s3_credentials_are_rejected(only):
    with pytest.raises(ValueError) as caught:
        DuckLakeSettings.from_env({**ENV, only: "x"})
    message = str(caught.value)
    assert "DUCKDB_S3_ACCESS_KEY_ID" in message and "DUCKDB_S3_SECRET_ACCESS_KEY" in message
    assert "both or neither" in message


def test_secret_texts_cover_every_spelling_of_the_credentials():
    s = DuckLakeSettings.from_env({
        **ENV, "DUCKDB_CATALOG_PASSWORD": "p w'd", "DUCKDB_S3_ACCESS_KEY_ID": "k",
        "DUCKDB_S3_SECRET_ACCESS_KEY": "s3cret",
    })
    texts = secret_texts(s)
    assert {"p w'd", "'p w\\'d'", "p w''d", "s3cret"} <= set(texts)
    assert list(texts) == sorted(texts, key=len, reverse=True)
    assert secret_texts(DuckLakeSettings.from_env(ENV)) == ()


def test_local_data_path_does_not_use_s3():
    assert not DuckLakeSettings.from_env({**ENV, "DUCKDB_DATA_PATH": "/data/lake/"}).uses_s3


def test_quoting_helpers():
    assert quote_identifier('we"ird') == '"we""ird"'
    assert quote_identifier("50%") == '"50%"'
    assert sql_literal("it's") == "'it''s'"


def test_attach_statement_plain():
    s = DuckLakeSettings.from_env({**ENV, "DUCKDB_CATALOG_PORT": "15599", "DUCKDB_CATALOG_PASSWORD": "continuo"})
    assert attach_statement(s, "lake") == (
        "ATTACH 'ducklake:postgres:host=localhost port=15599 dbname=catalog user=continuo "
        "password=continuo' AS \"lake\" (DATA_PATH 's3://warehouse/lake/')"
    )


def test_attach_statement_with_inlining_limit():
    s = DuckLakeSettings.from_env({**ENV, "DUCKDB_DATA_INLINING_ROW_LIMIT": "0"})
    assert attach_statement(s, "lake").endswith("(DATA_PATH 's3://warehouse/lake/', DATA_INLINING_ROW_LIMIT 0)")


def test_attach_statement_escapes_awkward_passwords():
    s = DuckLakeSettings.from_env({**ENV, "DUCKDB_CATALOG_PASSWORD": "p w'd\\x"})
    statement = attach_statement(s, "lake")
    # undo the SQL-literal doubling, then the libpq quoting must be intact
    assert "password='p w\\'d\\\\x'" in statement.replace("''", "'")
    # an empty password renders as libpq '' which the SQL literal doubles to ''''
    empty = DuckLakeSettings.from_env({**ENV, "DUCKDB_CATALOG_PASSWORD": ""})
    assert "password=''''" in attach_statement(empty, "lake")


def test_s3_secret_with_static_credentials():
    s = DuckLakeSettings.from_env({
        **ENV, "DUCKDB_S3_ENDPOINT": "localhost:19100", "DUCKDB_S3_USE_SSL": "false",
        "DUCKDB_S3_ACCESS_KEY_ID": "k'1", "DUCKDB_S3_SECRET_ACCESS_KEY": "s",
    })
    assert s3_secret_statement(s) == (
        "CREATE OR REPLACE SECRET continuo_s3 (TYPE S3, KEY_ID 'k''1', SECRET 's', "
        "ENDPOINT 'localhost:19100', REGION 'us-east-1', URL_STYLE 'path', USE_SSL false)"
    )


def test_s3_secret_falls_back_to_the_credential_chain():
    statement = s3_secret_statement(DuckLakeSettings.from_env(ENV))
    assert statement == (
        "CREATE OR REPLACE SECRET continuo_s3 (TYPE S3, PROVIDER credential_chain, "
        "REGION 'us-east-1', URL_STYLE 'vhost', USE_SSL true)"
    )


def test_extension_statements_quote_the_name():
    assert load_extension("ducklake") == 'LOAD "ducklake"'
    assert install_extension("ducklake") == 'INSTALL "ducklake"'
    assert load_extension('we"ird') == 'LOAD "we""ird"'


def test_set_extension_directory_escapes_the_path():
    assert set_extension_directory("/opt/duckdb") == "SET extension_directory = '/opt/duckdb'"
    assert set_extension_directory("/o'pt") == "SET extension_directory = '/o''pt'"


def test_use_catalog_quotes_the_alias():
    assert use_catalog("lake") == 'USE "lake"'
    assert use_catalog('la"ke') == 'USE "la""ke"'


def test_extensions_are_the_three_the_adapter_needs():
    assert EXTENSIONS == ("ducklake", "postgres", "httpfs")


def test_check_offline_fails_when_the_extensions_are_not_in_the_directory(tmp_path):
    with pytest.raises(duckdb.Error):
        check_offline(str(tmp_path))


R = DdlRenderer("lake")
T = QualifiedTable.of("s", "t")


def test_references_are_catalog_qualified_and_quoted():
    assert R.schema_ref(Identifier("s")) == '"lake"."s"'
    assert R.table_ref(T) == '"lake"."s"."t"'
    # a schema or table named like the catalog alias must not be ambiguous
    assert R.table_ref(QualifiedTable.of("lake", "lake")) == '"lake"."lake"."lake"'
    assert R.table_ref(QualifiedTable.of('we"ird', "50%")) == '"lake"."we""ird"."50%"'
    assert R.catalog_name == "lake"


def test_schema_and_table_ddl():
    assert R.create_schema_if_not_exists(Identifier("s")) == 'CREATE SCHEMA IF NOT EXISTS "lake"."s"'
    assert R.drop_schema_cascade(Identifier("s")) == 'DROP SCHEMA IF EXISTS "lake"."s" CASCADE'
    assert R.drop_table_if_exists(T) == 'DROP TABLE IF EXISTS "lake"."s"."t"'


def test_create_table_renders_types_and_not_null():
    cols = [ColumnDefinition(Identifier("id"), "INTEGER", False), ColumnDefinition(Identifier("ts"), "TIMESTAMP")]
    assert R.create_table(T, cols) == 'CREATE TABLE "lake"."s"."t" ("id" INTEGER NOT NULL, "ts" TIMESTAMP)'
    assert R.create_table(T, cols, if_not_exists=True).startswith('CREATE TABLE IF NOT EXISTS "lake"."s"."t" (')


def test_empty_builds():
    assert R.create_empty_table_as(T, "SELECT 1 AS a -- c") == (
        'CREATE TABLE "lake"."s"."t" AS (\nSELECT 1 AS a -- c\n) WITH NO DATA'
    )
    assert R.create_empty_clone(T, QualifiedTable.of("p", "t")) == (
        'CREATE TABLE "lake"."s"."t" AS SELECT * FROM "lake"."p"."t" WHERE false'
    )


def test_layout_ddl():
    keys = (PartitionKey(Identifier("name")), PartitionKey(Identifier("id"), "bucket", 4),
            PartitionKey(Identifier("ts"), "month"))
    assert R.set_partitioned_by(T, keys) == (
        'ALTER TABLE "lake"."s"."t" SET PARTITIONED BY ("name", bucket(4, "id"), month("ts"))'
    )
    sort = (SortKey(Identifier("id"), True, False), SortKey(Identifier("name")), SortKey(Identifier("ts"), False, True))
    assert R.set_sorted_by(T, sort) == (
        'ALTER TABLE "lake"."s"."t" SET SORTED BY ("id" DESC NULLS LAST, "name" ASC, "ts" ASC NULLS FIRST)'
    )


def test_read_and_write_statements():
    assert R.explain_read("SELECT 1 -- c") == "EXPLAIN SELECT * FROM (\nSELECT 1 -- c\n) AS __check_binds__"
    assert R.delete_all(T) == 'DELETE FROM "lake"."s"."t"'
    assert R.insert_select(T, ["a", "b%"], "src") == (
        'INSERT INTO "lake"."s"."t" ("a", "b%") SELECT "a", "b%" FROM "src"'
    )
