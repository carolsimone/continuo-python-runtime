"""Connection settings for one DuckLake, read from DUCKDB_* environment variables."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

REQUIRED_ENV: tuple[str, ...] = (
    "DUCKDB_CATALOG_HOST",
    "DUCKDB_CATALOG_DB",
    "DUCKDB_CATALOG_USER",
    "DUCKDB_DATA_PATH",
)
_URL_STYLES = ("path", "vhost")
_TRUE_WORDS = ("true", "1", "yes")
_FALSE_WORDS = ("false", "0", "no")


def _is_ascii_digits(value: str) -> bool:
    # str.isdigit() alone accepts e.g. superscripts, which int() then rejects.
    return value.isascii() and value.isdigit()


@dataclass(frozen=True)
class DuckLakeSettings:
    catalog_host: str
    catalog_port: str
    catalog_db: str
    catalog_user: str
    catalog_password: str = field(repr=False)
    data_path: str
    s3_endpoint: str | None
    s3_access_key_id: str | None
    s3_secret_access_key: str | None = field(repr=False)
    s3_region: str
    s3_url_style: str
    s3_use_ssl: bool
    extension_directory: str | None
    temp_directory: str | None
    data_inlining_row_limit: int | None

    @property
    def uses_s3(self) -> bool:
        return self.data_path.startswith("s3://")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DuckLakeSettings":
        env = os.environ if env is None else env

        def need(name: str) -> str:
            value = env.get(name, "")
            if not value:
                raise ValueError(f"missing required env var {name}")
            return value

        def optional(name: str) -> str | None:
            return env.get(name) or None

        port = env.get("DUCKDB_CATALOG_PORT") or "5432"
        if not _is_ascii_digits(port):
            raise ValueError(f"DUCKDB_CATALOG_PORT must be a port number, got {port!r}")
        limit_raw = optional("DUCKDB_DATA_INLINING_ROW_LIMIT")
        if limit_raw is not None and not _is_ascii_digits(limit_raw):
            raise ValueError(
                f"DUCKDB_DATA_INLINING_ROW_LIMIT must be a non-negative integer, got {limit_raw!r}"
            )
        endpoint = optional("DUCKDB_S3_ENDPOINT")
        url_style = env.get("DUCKDB_S3_URL_STYLE") or ("path" if endpoint else "vhost")
        if url_style not in _URL_STYLES:
            raise ValueError(f"DUCKDB_S3_URL_STYLE must be one of {_URL_STYLES}, got {url_style!r}")
        ssl_word = (env.get("DUCKDB_S3_USE_SSL") or "true").lower()
        if ssl_word not in _TRUE_WORDS + _FALSE_WORDS:
            raise ValueError(
                "DUCKDB_S3_USE_SSL must be one of "
                f"{', '.join(_TRUE_WORDS + _FALSE_WORDS)}, got {env.get('DUCKDB_S3_USE_SSL')!r}"
            )
        use_ssl = ssl_word in _TRUE_WORDS
        access_key_id = optional("DUCKDB_S3_ACCESS_KEY_ID")
        secret_access_key = optional("DUCKDB_S3_SECRET_ACCESS_KEY")
        if (access_key_id is None) != (secret_access_key is None):
            raise ValueError(
                "DUCKDB_S3_ACCESS_KEY_ID and DUCKDB_S3_SECRET_ACCESS_KEY: "
                "set both or neither"
            )
        return cls(
            catalog_host=need("DUCKDB_CATALOG_HOST"),
            catalog_port=port,
            catalog_db=need("DUCKDB_CATALOG_DB"),
            catalog_user=need("DUCKDB_CATALOG_USER"),
            catalog_password=env.get("DUCKDB_CATALOG_PASSWORD", ""),
            data_path=need("DUCKDB_DATA_PATH"),
            s3_endpoint=endpoint,
            s3_access_key_id=access_key_id,
            s3_secret_access_key=secret_access_key,
            s3_region=env.get("DUCKDB_S3_REGION") or "us-east-1",
            s3_url_style=url_style,
            s3_use_ssl=use_ssl,
            extension_directory=optional("DUCKDB_EXTENSION_DIRECTORY"),
            temp_directory=optional("DUCKDB_TEMP_DIRECTORY"),
            data_inlining_row_limit=None if limit_raw is None else int(limit_raw),
        )
