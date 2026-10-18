"""Minimal sd_notify(3): one AF_UNIX datagram to $NOTIFY_SOCKET. Stdlib only.

No-op (returns False) when NOTIFY_SOCKET is unset, i.e. when not run by systemd with
Type=notify, so the service and the tests work the same without systemd.
A leading "@" means a Linux abstract-namespace socket (sent with a leading NUL).
Errors are swallowed and logged at debug: a notification must never crash the service.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Mapping, Optional, Union

log = logging.getLogger(__name__)


def socket_address(raw: Optional[str]) -> Optional[Union[str, bytes]]:
    """$NOTIFY_SOCKET -> an address for socket.sendto, or None if unusable."""
    if not raw:
        return None
    if raw.startswith("@"):
        return b"\0" + raw[1:].encode()  # abstract namespace (Linux)
    if raw.startswith("/"):
        return raw
    return None  # e.g. "vsock:..." -- not used for a local service


def notify(*lines: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """Send "KEY=VALUE" lines, e.g. notify("READY=1", "STATUS=serving"). -> sent?"""
    env = os.environ if env is None else env
    addr = socket_address(env.get("NOTIFY_SOCKET"))
    if addr is None or not lines:
        return False
    msg = "\n".join(lines).encode("utf-8", "replace")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(msg, addr)
        return True
    except OSError as exc:
        log.debug("sd_notify %r failed: %s", lines, exc)
        return False


def watchdog_interval(env: Optional[Mapping[str, str]] = None) -> Optional[float]:
    """WatchdogSec= in seconds if systemd enabled the watchdog for THIS process."""
    env = os.environ if env is None else env
    usec = env.get("WATCHDOG_USEC")
    if not usec:
        return None
    pid = env.get("WATCHDOG_PID")
    if pid and pid.strip() != str(os.getpid()):
        return None
    try:
        v = int(usec) / 1e6
    except ValueError:
        return None
    return v if v > 0 else None


def ready(status: str = "") -> bool:
    return notify("READY=1", *([f"STATUS={status}"] if status else []))


def watchdog() -> bool:
    return notify("WATCHDOG=1")


def stopping() -> bool:
    return notify("STOPPING=1")


def status(text: str) -> bool:
    return notify(f"STATUS={text}")
