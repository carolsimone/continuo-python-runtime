"""Unit tests for ``PostgresAdapter.from_env``'s first connection.

A Job pod's first connection can be refused for a few seconds after the pod
starts, while the cluster's NetworkPolicy engine has not yet admitted the
pod; an engine that rejects (k3s's kube-router) refuses at once rather than
dropping packets. ``from_env`` retries only a connection the server never
answered, refused or unreachable, and raises every other failure at once.

The scripted cases raise ``psycopg2.OperationalError`` with libpq's own
message text (``pgcode`` is ``None`` for every connection failure, so the text
is all there is). One case connects to a local port nothing listens on, so it
checks the classification against what the installed libpq really says.
"""
import logging
import socket

import psycopg2  # type: ignore[import-untyped]
import pytest

import continuo_postgres_adapter.adapter as adapter_module
from continuo_postgres_adapter.adapter import PostgresAdapter

REFUSED = (
    'connection to server at "continuo-pg-rw" (10.43.101.94), port 5432 failed: '
    "Connection refused\n\tIs the server running on that host and accepting TCP/IP connections?\n"
)
# libpq 13 and older word a refused connection this way.
REFUSED_LIBPQ_13 = (
    "could not connect to server: Connection refused\n\tIs the server running on host "
    '"continuo-pg-rw" (10.43.101.94) and accepting\n\tTCP/IP connections on port 5432?\n'
)
NO_ROUTE = (
    'connection to server at "continuo-pg-rw" (10.43.101.94), port 5432 failed: '
    "No route to host\n\tIs the server running on that host and accepting TCP/IP connections?\n"
)
NET_UNREACHABLE = (
    'connection to server at "continuo-pg-rw" (10.43.101.94), port 5432 failed: '
    "Network is unreachable\n\tIs the server running on that host and accepting TCP/IP connections?\n"
)
AUTH_FAILED = (
    'connection to server at "continuo-pg-rw" (10.43.101.94), port 5432 failed: '
    'FATAL:  password authentication failed for user "continuo"\n'
)
NO_DATABASE = (
    'connection to server at "continuo-pg-rw" (10.43.101.94), port 5432 failed: '
    'FATAL:  database "warehouse" does not exist\n'
)
TIMED_OUT = 'connection to server at "continuo-pg-rw" (10.43.101.94), port 5432 failed: timeout expired\n'
UNKNOWN_HOST = (
    'could not translate host name "continuo-pg-rw" to address: Name or service not known\n'
)
# A host name with two addresses: the server answered on the second, so the
# failure is the server's verdict, not an unreachable host.
REFUSED_THEN_AUTH_FAILED = (
    'connection to server at "localhost" (::1), port 5432 failed: Connection refused\n'
    "\tIs the server running on that host and accepting TCP/IP connections?\n"
    'connection to server at "localhost" (127.0.0.1), port 5432 failed: '
    'FATAL:  password authentication failed for user "continuo"\n'
)
# libpq reports every address it tried in one message. One address that stayed
# silent means the host is not just unadmitted yet, and a retry would wait out
# its timeout again (libpq 17's text, a real dual-stack localhost).
REFUSED_THEN_TIMED_OUT = (
    'connection to server at "localhost" (::1), port 5432 failed: Connection refused\n'
    "\tIs the server running on that host and accepting TCP/IP connections?\n"
    'connection to server at "localhost" (127.0.0.1), port 5432 failed: timeout expired\n'
)
TIMED_OUT_THEN_REFUSED = (
    'connection to server at "localhost" (127.0.0.1), port 5432 failed: timeout expired\n'
    'connection to server at "localhost" (::1), port 5432 failed: Connection refused\n'
    "\tIs the server running on that host and accepting TCP/IP connections?\n"
)
# libpq 13 and earlier print a timeout as a bare line.
REFUSED_THEN_TIMED_OUT_LIBPQ_13 = (
    "could not connect to server: Connection refused\n\tIs the server running on host "
    '"localhost" (::1) and accepting\n\tTCP/IP connections on port 5432?\n'
    "timeout expired\n"
)
REFUSED_TWICE = (
    'connection to server at "localhost" (::1), port 5432 failed: Connection refused\n'
    "\tIs the server running on that host and accepting TCP/IP connections?\n"
    'connection to server at "localhost" (127.0.0.1), port 5432 failed: Connection refused\n'
    "\tIs the server running on that host and accepting TCP/IP connections?\n"
)


class _Connection:
    """What PostgresAdapter.__init__ touches of a psycopg2 connection."""

    autocommit = False

    def close(self):
        pass


class _ConnectScript:
    """psycopg2.connect replacement: raises the next scripted error (None = connect)."""

    def __init__(self, errors):
        self.errors = list(errors)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        error = self.errors.pop(0) if self.errors else None
        if error is not None:
            raise psycopg2.OperationalError(error)
        return _Connection()


@pytest.fixture
def postgres_env(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "continuo-pg-rw")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_DB", "warehouse")
    monkeypatch.setenv("POSTGRES_USER", "continuo")
    monkeypatch.setenv("POSTGRES_PASSWORD", "s3cret")


@pytest.fixture
def connect_with(monkeypatch, postgres_env):
    def run(errors):
        script = _ConnectScript(errors)
        monkeypatch.setattr(adapter_module.psycopg2, "connect", script)
        return script

    return run


@pytest.mark.parametrize("message", [REFUSED, REFUSED_LIBPQ_13, NO_ROUTE, NET_UNREACHABLE, REFUSED_TWICE],
                         ids=["refused", "refused-libpq-13", "no-route", "net-unreachable", "refused-twice"])
def test_an_unreachable_server_is_retried_until_it_answers(connect_with, message):
    script = connect_with([message, message, None])
    sleeps: list[float] = []

    adapter = PostgresAdapter.from_env(sleep=sleeps.append)

    assert isinstance(adapter, PostgresAdapter)
    assert len(script.calls) == 3
    assert sleeps == [2.0, 2.0]


def test_every_retry_connects_with_the_same_parameters(connect_with):
    script = connect_with([REFUSED, None])

    PostgresAdapter.from_env(sleep=lambda _seconds: None)

    expected = dict(host="continuo-pg-rw", port="5432", dbname="warehouse", user="continuo", password="s3cret")
    assert script.calls == [expected, expected]


def test_a_server_that_stays_unreachable_fails_after_five_attempts(connect_with):
    script = connect_with([REFUSED] * 50)
    sleeps: list[float] = []

    with pytest.raises(psycopg2.OperationalError, match="Connection refused"):
        PostgresAdapter.from_env(sleep=sleeps.append)

    assert len(script.calls) == 5
    assert sleeps == [2.0, 2.0, 2.0, 2.0]


def test_each_retry_is_logged_with_the_drivers_message(connect_with, caplog):
    connect_with([REFUSED, REFUSED, None])
    caplog.set_level(logging.INFO, logger="continuo_postgres_adapter")

    PostgresAdapter.from_env(sleep=lambda _seconds: None)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert all("port 5432 failed: Connection refused" in r.getMessage() for r in warnings)


@pytest.mark.parametrize("message", [
    AUTH_FAILED, NO_DATABASE, TIMED_OUT, UNKNOWN_HOST, REFUSED_THEN_AUTH_FAILED,
    REFUSED_THEN_TIMED_OUT, TIMED_OUT_THEN_REFUSED, REFUSED_THEN_TIMED_OUT_LIBPQ_13,
], ids=["auth-failed", "no-database", "timed-out", "unknown-host", "refused-then-auth-failed",
        "refused-then-timed-out", "timed-out-then-refused", "refused-then-timed-out-libpq-13"])
def test_every_other_connection_failure_is_raised_at_once(connect_with, message):
    script = connect_with([message, None])
    sleeps: list[float] = []

    with pytest.raises(psycopg2.OperationalError) as caught:
        PostgresAdapter.from_env(sleep=sleeps.append)

    assert str(caught.value) == message
    assert len(script.calls) == 1 and sleeps == []


def _free_port() -> int:
    """A loopback port no socket holds, so a connection to it is refused.

    A port merely bound, without listen(), is not enough: macOS drops the
    connection's SYN instead of refusing it, and the connect times out.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_a_real_refused_connection_is_retried(monkeypatch, postgres_env):
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_PORT", str(_free_port()))
    sleeps: list[float] = []

    with pytest.raises(psycopg2.OperationalError, match="Connection refused"):
        PostgresAdapter.from_env(sleep=sleeps.append)

    assert sleeps == [2.0, 2.0, 2.0, 2.0]
