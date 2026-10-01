"""The catalog-outage test double must really be an outage, on every platform.

The mid-session outage integration tests put a TCP proxy in front of the
catalog and "cut" it. On Linux, closing a listening socket does not release it
while another thread is blocked in ``accept()``: the kernel kept accepting, the
accept loop kept proxying, DuckDB reconnected through the "outage", and the
tests passed on macOS but failed (correctly: they assert the outage error) on
the Linux CI runner. This test pins the proxy's own contract, with no stack.
"""
import socket
import time

import pytest


def test_the_catalog_proxy_refuses_new_connections_after_a_cut(catalog_proxy):
    time.sleep(0.2)  # let the accept thread block in accept(): the Linux failure needs it
    catalog_proxy.cut()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", catalog_proxy.port), timeout=2)


def test_the_catalog_proxy_can_be_cut_twice(catalog_proxy):
    catalog_proxy.cut()
    catalog_proxy.cut()
