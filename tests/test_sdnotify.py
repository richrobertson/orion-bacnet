"""water_bacnet.sdnotify against a fake $NOTIFY_SOCKET (AF_UNIX datagram)."""

import os
import socket
import sys
import tempfile

import pytest

from water_bacnet import sdnotify


@pytest.fixture
def notify_socket(monkeypatch):
    # AF_UNIX paths are limited to ~104 bytes on macOS: keep it short.
    d = tempfile.mkdtemp(prefix="sdn", dir="/tmp" if os.path.isdir("/tmp") else None)
    path = os.path.join(d, "n.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(path)
    s.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    try:
        yield s
    finally:
        s.close()
        os.unlink(path)
        os.rmdir(d)


def test_no_socket_is_a_noop(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sdnotify.notify("READY=1") is False
    assert sdnotify.ready() is False
    assert sdnotify.watchdog() is False


def test_ready_and_status(notify_socket):
    assert sdnotify.ready("serving on 192.0.2.5") is True
    assert notify_socket.recv(4096) == b"READY=1\nSTATUS=serving on 192.0.2.5"
    assert sdnotify.watchdog() is True
    assert notify_socket.recv(4096) == b"WATCHDOG=1"
    assert sdnotify.stopping() is True
    assert notify_socket.recv(4096) == b"STOPPING=1"


def test_explicit_env_mapping(notify_socket):
    path = os.environ["NOTIFY_SOCKET"]
    assert sdnotify.notify("STATUS=x", env={}) is False
    assert sdnotify.notify("STATUS=x", env={"NOTIFY_SOCKET": path}) is True
    assert notify_socket.recv(4096) == b"STATUS=x"


def test_dead_socket_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "gone.sock"))
    assert sdnotify.notify("READY=1") is False


def test_socket_address_forms():
    assert sdnotify.socket_address(None) is None
    assert sdnotify.socket_address("") is None
    assert sdnotify.socket_address("/run/systemd/notify") == "/run/systemd/notify"
    assert sdnotify.socket_address("@/org/freedesktop/systemd1/notify") == \
        b"\0/org/freedesktop/systemd1/notify"
    assert sdnotify.socket_address("vsock:2:1234") is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="abstract sockets are Linux-only")
def test_abstract_namespace_socket(monkeypatch):
    name = f"water-bacnet-test-{os.getpid()}"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind("\0" + name)
    s.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", "@" + name)
    try:
        assert sdnotify.ready() is True
        assert s.recv(100) == b"READY=1"
    finally:
        s.close()


def test_watchdog_interval(monkeypatch):
    assert sdnotify.watchdog_interval({}) is None
    assert sdnotify.watchdog_interval({"WATCHDOG_USEC": "120000000"}) == 120.0
    assert sdnotify.watchdog_interval({"WATCHDOG_USEC": "120000000",
                                       "WATCHDOG_PID": str(os.getpid())}) == 120.0
    assert sdnotify.watchdog_interval({"WATCHDOG_USEC": "120000000", "WATCHDOG_PID": "1"}) is None
    assert sdnotify.watchdog_interval({"WATCHDOG_USEC": "x"}) is None
    assert sdnotify.watchdog_interval({"WATCHDOG_USEC": "0"}) is None


# ---- the BACnet refresh loop's use of it (no sockets bound: bacnet internals only)

bacnet = pytest.importorskip("water_bacnet.bacnet")


class _Src:
    def __init__(self, hb):
        self.hb = hb

    def heartbeat(self):
        return self.hb

    def latest(self, now=None):
        from water_bacnet.meter import Reading
        return Reading.empty("test")


def _in_loop(fn):
    """bacpypes3 objects need a running event loop to be constructed."""
    import asyncio

    async def go():
        return fn()
    return asyncio.run(go())


def _server(src):
    from water_bacnet.config import load_config
    return bacnet.MeterBacnetServer(load_config(None), src)


def test_watchdog_only_while_source_loop_alive(notify_socket, caplog):
    import time
    src = _Src(time.monotonic())
    srv = _in_loop(lambda: _server(src))
    assert srv.watchdog_ping() is True
    assert notify_socket.recv(100) == b"WATCHDOG=1"
    src.hb = time.monotonic() - 61  # source loop stuck for > 60 s
    assert srv.watchdog_ping() is False
    assert srv.watchdog_ping() is False
    notify_socket.settimeout(0.2)
    with pytest.raises(socket.timeout):
        notify_socket.recv(100)
    assert sum("withholding" in r.getMessage() for r in caplog.records) == 1  # logged once
    src.hb = time.monotonic()
    assert srv.watchdog_ping() is True
    notify_socket.settimeout(2)
    assert notify_socket.recv(100) == b"WATCHDOG=1"


def test_source_without_heartbeat_counts_as_alive(notify_socket):
    class Old:
        def latest(self, now=None):
            return None
    assert _in_loop(lambda: _server(Old())).watchdog_ping() is True


def test_tick_respects_watchdog_usec(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "30000000")
    monkeypatch.delenv("WATCHDOG_PID", raising=False)
    assert _in_loop(lambda: _server(_Src(0))).tick_s <= 10.0
