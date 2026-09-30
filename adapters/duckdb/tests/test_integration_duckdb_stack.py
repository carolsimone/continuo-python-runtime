"""The adapter connects to the real DuckLake stack, and fails cleanly when it cannot."""
import logging
import traceback
import uuid

import duckdb
import pytest

pytestmark = pytest.mark.integration


def test_from_env_connects_and_serves_a_read(adapter):
    assert adapter.fetch("SELECT 41 + 1 AS answer").to_pylist() == [{"answer": 42}]


def test_ensure_schema_creates_a_schema_visible_to_another_connection(adapter, adapter_factory, schema):
    adapter.ensure_schema(schema)
    other = adapter_factory()
    other.ensure_table(schema, "t", [{"name": "id", "type": "INTEGER", "nullable": True}], config={})
    assert other.fetch(f'SELECT count(*) AS n FROM "{schema}"."t"').to_pylist() == [{"n": 0}]


def test_unreachable_catalog_raises_a_clear_error(adapter_factory):
    with pytest.raises(duckdb.Error):
        adapter_factory(DUCKDB_CATALOG_PORT="1")


def test_wrong_catalog_password_raises_a_clear_error(adapter_factory):
    with pytest.raises(duckdb.Error):
        adapter_factory(DUCKDB_CATALOG_PASSWORD="definitely-wrong")


def test_failed_attach_does_not_echo_the_password(adapter_factory):
    wrong = "wrong-pw-Zx81"
    with pytest.raises(duckdb.Error) as caught:
        adapter_factory(DUCKDB_CATALOG_PASSWORD=wrong)
    assert wrong not in str(caught.value)


def _outage_operations():
    import pyarrow as pa

    return {
        "ensure_schema": lambda a: a.ensure_schema(f"outage_{uuid.uuid4().hex[:8]}"),
        "fetch": lambda a: a.fetch("SELECT count(*) FROM information_schema.tables"),
        "load": lambda a: a.load("no_such_schema", "t", pa.table({"a": [1]})),
        "check_binds": lambda a: a.check_binds("SELECT * FROM information_schema.tables"),
    }


def _assert_outage_is_clean(adapter, catalog_proxy, caplog, password, *, bare):
    """Cut the catalog mid-session; no error text and no log record may carry *password*.

    *bare* also forbids the password token on its own (only sound for a password
    distinctive enough not to occur in paths or in the user name).
    """
    adapter.fetch("SELECT count(*) FROM information_schema.schemata")
    catalog_proxy.cut()
    texts: list[str] = []
    with caplog.at_level(logging.DEBUG):
        for operation in _outage_operations().values():
            try:
                operation(adapter)
            except Exception as exc:  # noqa: BLE001 - any error type must be clean
                texts.append(f"{type(exc).__name__}: {exc}")
                texts.append("".join(traceback.format_exception(exc)))
    texts.extend(record.getMessage() for record in caplog.records)
    assert any("Unable to connect to Postgres" in text for text in texts), texts
    for text in texts:
        assert f"password={password}" not in text, text
        if bare:
            assert password not in text, text


def test_catalog_outage_mid_session_does_not_leak_the_password(adapter_factory, catalog_proxy, caplog):
    adapter = adapter_factory(DUCKDB_CATALOG_HOST="127.0.0.1", DUCKDB_CATALOG_PORT=str(catalog_proxy.port))
    _assert_outage_is_clean(adapter, catalog_proxy, caplog, "continuo", bare=False)


def test_redaction_alone_covers_a_password_a_passfile_cannot_hold(
    fresh_catalog, adapter_factory, catalog_proxy, caplog
):
    """A newline cannot live in a passfile, so this password travels inline in the DSN."""
    env = fresh_catalog(password="Zx-leak-9\nprobe'q")
    adapter = adapter_factory(
        **{**env, "DUCKDB_CATALOG_HOST": "127.0.0.1", "DUCKDB_CATALOG_PORT": str(catalog_proxy.port)}
    )
    _assert_outage_is_clean(adapter, catalog_proxy, caplog, "Zx-leak-9", bare=True)


def test_the_password_is_not_visible_in_the_attached_database_path(adapter):
    path = adapter.fetch(
        "SELECT path FROM duckdb_databases() WHERE database_name = 'lake'"
    ).to_pylist()[0]["path"]
    assert "password=" not in path


def test_temp_directory_setting_reaches_the_engine(adapter_factory, tmp_path):
    adapter = adapter_factory(DUCKDB_TEMP_DIRECTORY=str(tmp_path))
    setting = adapter.fetch("SELECT current_setting('temp_directory') AS d").to_pylist()[0]["d"]
    assert setting == str(tmp_path)


def test_concurrent_first_attach_of_a_fresh_catalog_all_succeed(fresh_catalog):
    """Eight Jobs starting together against a never-used catalog must all attach."""
    import concurrent.futures

    from continuo_duckdb_adapter.infrastructure.session import DuckLakeSession
    from continuo_duckdb_adapter.infrastructure.settings import DuckLakeSettings

    settings = DuckLakeSettings.from_env(fresh_catalog())

    def attach(_: int) -> str:
        try:
            DuckLakeSession.connect(settings).close()
            return "ok"
        except Exception as exc:  # noqa: BLE001 - report every failure
            return f"{type(exc).__name__}: {str(exc)[:200]}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attach, range(8)))
    assert results == ["ok"] * 8
