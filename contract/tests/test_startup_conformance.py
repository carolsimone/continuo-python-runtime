"""StartupConformance and its LateOpeningPort, run against a reference adapter.

The reference adapter connects a TCP socket to a ping/pong server: it shows a
bring-your-own adapter how to provide the two fixtures, and two broken
adapters check that the suite catches an adapter that connects once and one
that connects lazily, without retrying, on its first request.
"""
import contextlib
import os
import socket
import threading
import time

import pyarrow as pa
import pytest

from continuo_engine_contract.testing.conformance import LateOpeningPort, StartupConformance


@pytest.fixture
def warehouse_address():
    """A ping/pong server standing in for a warehouse."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                while conn.recv(4) == b"ping":
                    conn.sendall(b"pong")

    threading.Thread(target=serve, daemon=True).start()
    yield server.getsockname()
    with server, contextlib.suppress(OSError):
        # Wakes the accept() on Linux; macOS refuses shutdown() on a listener.
        server.shutdown(socket.SHUT_RDWR)


def _address():
    return os.environ["REFERENCE_HOST"], int(os.environ["REFERENCE_PORT"])


class _RetryingWarehouse:
    """Connects in from_env and retries a refused connection: what the suite requires."""

    drop_semantics = "late_binding"

    def __init__(self, sock):
        self._sock = sock

    @classmethod
    def from_env(cls):
        for attempt in range(1, 6):
            try:
                return cls(socket.create_connection(_address(), timeout=5))
            except ConnectionRefusedError:
                if attempt == 5:
                    raise
                time.sleep(1.0)

    def fetch(self, sql):
        if self._sock is None:
            self._sock = socket.create_connection(_address(), timeout=5)
        self._sock.sendall(b"ping")
        if self._sock.recv(4) != b"pong":
            raise RuntimeError("the warehouse did not answer")
        return pa.table({"one": [1]})

    def close(self):
        if self._sock is not None:
            self._sock.close()


class _ConnectOnceWarehouse(_RetryingWarehouse):
    @classmethod
    def from_env(cls):
        return cls(socket.create_connection(_address(), timeout=5))


class _LazyWarehouse(_RetryingWarehouse):
    """Opens no connection until the first request, and does not retry it."""

    @classmethod
    def from_env(cls):
        return cls(None)


class _UndeclaredDropSemanticsWarehouse(_RetryingWarehouse):
    drop_semantics = "tombstone"


def _from_env_of(adapter_cls, monkeypatch):
    def build(host, port):
        monkeypatch.setenv("REFERENCE_HOST", host)
        monkeypatch.setenv("REFERENCE_PORT", str(port))
        return adapter_cls.from_env()

    return build


@pytest.fixture
def adapter_from_env(monkeypatch):
    return _from_env_of(_RetryingWarehouse, monkeypatch)


class TestReferenceStartup(StartupConformance):
    pass


def test_the_suite_catches_an_adapter_that_connects_once(warehouse_address, monkeypatch):
    with pytest.raises(ConnectionRefusedError):
        StartupConformance().test_from_env_waits_out_a_warehouse_that_refuses_at_first(
            warehouse_address, _from_env_of(_ConnectOnceWarehouse, monkeypatch)
        )


def test_the_suite_catches_an_adapter_that_connects_lazily_without_retrying(
    warehouse_address, monkeypatch
):
    with pytest.raises(ConnectionRefusedError):
        StartupConformance().test_from_env_waits_out_a_warehouse_that_refuses_at_first(
            warehouse_address, _from_env_of(_LazyWarehouse, monkeypatch)
        )


def test_the_suite_catches_an_adapter_with_an_unknown_drop_semantics(warehouse_address, monkeypatch):
    with pytest.raises(AssertionError, match="tombstone"):
        StartupConformance().test_declares_a_drop_semantics(
            warehouse_address, _from_env_of(_UndeclaredDropSemanticsWarehouse, monkeypatch)
        )


# --- the LateOpeningPort double itself ---------------------------------------


def _ping(port):
    with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
        conn.sendall(b"ping")
        return conn.recv(4)


def test_the_port_refuses_connections_before_it_opens(warehouse_address):
    with LateOpeningPort(warehouse_address, refuse_for=60) as port:
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port.port), timeout=2)


def test_the_port_forwards_once_it_opens(warehouse_address):
    with LateOpeningPort(warehouse_address, refuse_for=0.2) as port:
        deadline = time.monotonic() + 5
        while True:
            try:
                assert _ping(port.port) == b"pong"
                break
            except ConnectionRefusedError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)


def test_the_port_refuses_connections_after_it_closes(warehouse_address):
    port = LateOpeningPort(warehouse_address, refuse_for=0)
    deadline = time.monotonic() + 5
    while True:
        try:
            _ping(port.port)
            break
        except ConnectionRefusedError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    time.sleep(0.2)  # let the accept thread block in accept(): the Linux case needs it
    port.close()
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port.port), timeout=2)
