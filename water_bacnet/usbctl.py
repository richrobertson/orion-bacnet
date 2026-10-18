"""Small, fakeable USB / power primitives for the recovery ladder.

These run ONLY inside the root helper (water_bacnet.helper); the unprivileged service
never calls them. Each function either returns a short human-readable detail string or
raises (OSError / LookupError / RuntimeError) with a message fit for the journal.

Also holds DEFAULTS, the single table of defaults for the config keys the radio side
reads ([storage] raw_*, save_interval_s and [recovery]); it is shared by the service
(sources.cfg_get) and the helper so both agree before and after config.py knows them.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
from pathlib import Path
from typing import Callable, Optional, Sequence

# _IO('U', 20): usbdevfs reset, from linux/usbdevice_fs.h
USBDEVFS_RESET = 0x5514

DEFAULTS = {
    ("storage", "raw_log"): True,
    ("storage", "raw_retention_days"): 365,
    ("storage", "save_interval_s"): 900.0,
    ("recovery", "usb_reset"): True,
    ("recovery", "power_cycle"): True,
    ("recovery", "uhubctl_path"): "uhubctl",
    ("recovery", "hub_location"): "1-1",  # Pi 4: all ports switch together
    ("recovery", "hub_port"): "",  # "" = all ports at hub_location
    ("recovery", "cycle_delay_s"): 5,
    ("recovery", "reboot"): False,
    ("recovery", "usb_vid_pid"): "0bda:2838",
    ("recovery", "max_power_cycles_per_day"): 4,
    ("recovery", "max_reboots_per_week"): 1,
    ("recovery", "step_timeout_s"): 120.0,
}


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def find_dongle(vid_pid: str, serial: Optional[str] = None,
                sysfs_root: str = "/sys/bus/usb/devices") -> tuple:
    """(busnum, devnum) of the USB device with this "vvvv:pppp" id.

    With `serial`, the device whose iSerial matches wins; if none matches but exactly
    one device has the vid:pid, that one is used (a re-flashed EEPROM must not stop a
    reset). Raises LookupError when nothing suitable is found.
    """
    want_vid, _, want_pid = vid_pid.lower().partition(":")
    matches = []
    root = Path(sysfs_root)
    try:
        entries = sorted(root.iterdir())
    except OSError as exc:
        raise LookupError(f"cannot list {sysfs_root}: {exc}")
    for entry in entries:
        if ":" in entry.name:  # interfaces (1-1.2:1.0), not devices
            continue
        vid, pid = _read(entry / "idVendor"), _read(entry / "idProduct")
        if vid is None or pid is None or vid.lower() != want_vid or pid.lower() != want_pid:
            continue
        bus, dev = _read(entry / "busnum"), _read(entry / "devnum")
        try:
            loc = (int(bus), int(dev))
        except (TypeError, ValueError):
            continue
        matches.append((loc, _read(entry / "serial"), entry.name))
    if not matches:
        raise LookupError(f"no USB device {vid_pid} under {sysfs_root}")
    if serial:
        for loc, ser, _name in matches:
            if ser == serial:
                return loc
        if len(matches) == 1:
            return matches[0][0]
        raise LookupError(
            f"{len(matches)} devices {vid_pid}, none with serial {serial!r}")
    if len(matches) > 1:
        raise LookupError(f"{len(matches)} devices {vid_pid} and no serial to pick one")
    return matches[0][0]


def usb_reset(bus: int, dev: int, devroot: str = "/dev/bus/usb",
              ioctl: Callable = fcntl.ioctl) -> str:
    """USBDEVFS_RESET on /dev/bus/usb/BBB/DDD (a port reset + re-enumeration)."""
    path = os.path.join(devroot, f"{int(bus):03d}", f"{int(dev):03d}")
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
    try:
        ioctl(fd, USBDEVFS_RESET, 0)
    finally:
        os.close(fd)
    return f"USBDEVFS_RESET on {path}"


def power_cycle_argv(uhubctl_path: str, location: str, port: str, delay) -> list:
    argv = [uhubctl_path, "-l", str(location)]
    if port:
        argv += ["-p", str(port)]
    argv += ["-a", "cycle", "-d", str(int(delay))]
    return argv


def _run(argv: Sequence[str], timeout: float, run: Callable) -> str:
    try:
        cp = run(list(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                 stdin=subprocess.DEVNULL, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{argv[0]} timed out after {timeout:.0f}s")
    except OSError as exc:
        raise RuntimeError(f"cannot run {argv[0]}: {exc}")
    out = cp.stdout.decode(errors="replace") if isinstance(cp.stdout, bytes) else (cp.stdout or "")
    tail = " | ".join(out.strip().splitlines()[-3:])
    if cp.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} exited {cp.returncode}: {tail}")
    return tail


def power_cycle(uhubctl_path: str, location: str, port: str = "", delay=5,
                run: Callable = subprocess.run) -> str:
    """Cut VBUS on the hub port(s) for `delay` s and restore it (a remote replug)."""
    argv = power_cycle_argv(uhubctl_path, location, port, delay)
    out = _run(argv, timeout=float(int(delay)) + 30.0, run=run)
    return f"{' '.join(argv)}: {out}" if out else " ".join(argv)


def request_reboot(systemctl: str = "systemctl", run: Callable = subprocess.run) -> str:
    """Ask systemd for a clean reboot (helper only)."""
    argv = [systemctl, "reboot"]
    _run(argv, timeout=60.0, run=run)
    return "systemctl reboot accepted"
