"""Unit tests for ``TrinoAdapter.from_env``'s first connection.

The trino DBAPI opens no connection until the first request, so ``from_env``
sends one (``SELECT 1``) to return a connected adapter. A Job pod's first
connection can be refused for a few seconds after the pod starts, while the
cluster's NetworkPolicy engine has not yet admitted the pod; ``from_env``
retries that request only when the coordinator never answered, refused or
unreachable, and raises every other failure at once.

The client wraps the socket error several times (``TrinoConnectionError``,
raised while handling ``requests``' ``ConnectionError``, raised from urllib3's
errors, raised from the ``OSError``). The scripted cases build that chain; one
case runs the real client against a local port nothing listens on.
"""
import errno
import logging
import os
import socket

import pytest
import requests
import trino
import trino.client
import trino.exceptions

import continuo_trino_adapter.adapter as adapter_module
from continuo_trino_adapter.adapter import TrinoAdapter


def _caused_by(outer: BaseException, *inner: BaseException) -> BaseException:
    """*outer*, raised while handling inner[0], itself raised while handling inner[1], ..."""
    for wrapper, cause in zip((outer, *inner), inner):
        wrapper.__context__ = cause
    return outer


def _connection_error(os_error: OSError) -> BaseException:
    return _caused_by(
        trino.exceptions.TrinoConnectionError("failed to execute: Max retries exceeded with url: /v1/statement"),
        requests.exceptions.ConnectionError("Max retries exceeded with url: /v1/statement"),
        os_error,
    )


def refused():
    return _connection_error(OSError(errno.ECONNREFUSED, os.strerror(errno.ECONNREFUSED)))


def no_route():
    return _connection_error(OSError(errno.EHOSTUNREACH, os.strerror(errno.EHOSTUNREACH)))


def net_unreachable():
    return _connection_error(OSError(errno.ENETUNREACH, os.strerror(errno.ENETUNREACH)))


def timed_out():
    return _caused_by(
        trino.exceptions.TrinoConnectionError("failed to execute: Connection to trino timed out"),
        requests.exceptions.ConnectTimeout("Connection to trino timed out. (connect timeout=30)"),
        TimeoutError("timed out"),
    )


def unknown_host():
    return _connection_error(socket.gaierror(socket.EAI_NONAME, "Name or service not known"))


def unauthorized():
    return trino.exceptions.HttpError("error 401: b'Unauthorized'")


class _Cursor:
    def __init__(self, connection):
        self._connection = connection

    def execute(self, statement, params=None):
        self._connection.statements.append(statement)
        error = self._connection.errors.pop(0) if self._connection.errors else None
        if error is not None:
            raise error

    def fetchall(self):
        return [[1]]

    def close(self):
        pass


class _Connection:
    """trino.dbapi.connect replacement: each request raises the next scripted error (None = answer)."""

    def __init__(self, errors):
        self.errors = list(errors)
        self.statements: list[str] = []
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True


@pytest.fixture
def trino_env(monkeypatch):
    monkeypatch.setenv("TRINO_HOST", "trino")
    monkeypatch.setenv("TRINO_PORT", "8080")
    monkeypatch.setenv("TRINO_CATALOG", "iceberg")
    monkeypatch.delenv("TRINO_PASSWORD", raising=False)
    monkeypatch.delenv("TRINO_HTTP_SCHEME", raising=False)


@pytest.fixture
def connect_with(monkeypatch, trino_env):
    def run(errors):
        connection = _Connection(errors)
        monkeypatch.setattr(adapter_module.trino.dbapi, "connect", lambda **_kwargs: connection)
        return connection

    return run


@pytest.mark.parametrize("error", [refused, no_route, net_unreachable],
                         ids=["refused", "no-route", "net-unreachable"])
def test_an_unreachable_coordinator_is_retried_until_it_answers(connect_with, error):
    connection = connect_with([error(), error(), None])
    sleeps: list[float] = []

    adapter = TrinoAdapter.from_env(sleep=sleeps.append)

    assert isinstance(adapter, TrinoAdapter)
    assert connection.statements == ["SELECT 1", "SELECT 1", "SELECT 1"]
    assert sleeps == [2.0, 2.0]
    assert not connection.closed


def test_a_coordinator_that_answers_at_once_takes_one_request(connect_with):
    connection = connect_with([])
    sleeps: list[float] = []

    TrinoAdapter.from_env(sleep=sleeps.append)

    assert connection.statements == ["SELECT 1"] and sleeps == []


def test_a_coordinator_that_stays_unreachable_fails_after_five_attempts(connect_with):
    connection = connect_with([refused() for _ in range(50)])
    sleeps: list[float] = []

    with pytest.raises(trino.exceptions.TrinoConnectionError):
        TrinoAdapter.from_env(sleep=sleeps.append)

    assert len(connection.statements) == 5
    assert sleeps == [2.0, 2.0, 2.0, 2.0]
    assert connection.closed


def test_each_retry_is_logged_with_the_clients_message(connect_with, caplog):
    connect_with([refused(), refused(), None])
    caplog.set_level(logging.INFO, logger="continuo_trino_adapter")

    TrinoAdapter.from_env(sleep=lambda _seconds: None)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert all("Max retries exceeded" in r.getMessage() for r in warnings)


@pytest.mark.parametrize("error", [timed_out, unknown_host, unauthorized],
                         ids=["timed-out", "unknown-host", "unauthorized"])
def test_every_other_failure_is_raised_at_once(connect_with, error):
    raised = error()
    connection = connect_with([raised, None])
    sleeps: list[float] = []

    with pytest.raises(type(raised)) as caught:
        TrinoAdapter.from_env(sleep=sleeps.append)

    assert caught.value is raised
    assert len(connection.statements) == 1 and sleeps == []
    assert connection.closed


def test_a_refusal_that_took_a_timeout_to_arrive_is_raised_at_once(connect_with):
    # urllib3 tries every address of the host and raises only the last one's
    # error: a refusal reported after 31 s hides an address that timed out.
    raised = refused()
    connection = connect_with([raised, None])
    sleeps: list[float] = []
    clock = iter([0.0, 31.0])

    with pytest.raises(trino.exceptions.TrinoConnectionError) as caught:
        TrinoAdapter.from_env(sleep=sleeps.append, clock=lambda: next(clock))

    assert caught.value is raised
    assert len(connection.statements) == 1 and sleeps == []


def _free_port() -> int:
    """A loopback port no socket holds, so a connection to it is refused."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_a_real_refused_connection_is_retried(monkeypatch, trino_env):
    monkeypatch.setenv("TRINO_HOST", "127.0.0.1")
    monkeypatch.setenv("TRINO_PORT", str(_free_port()))
    # The client's own backoff between its internal attempts, not from_env's.
    monkeypatch.setattr(trino.client, "sleep", lambda _seconds: None)
    sleeps: list[float] = []

    with pytest.raises(trino.exceptions.TrinoConnectionError, match="Connection refused"):
        TrinoAdapter.from_env(sleep=sleeps.append)

    assert sleeps == [2.0, 2.0, 2.0, 2.0]
