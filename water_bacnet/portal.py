"""Web configuration portal: python -m water_bacnet.portal [--config PATH]

A separate process (water-bacnet-portal.service) so it keeps working while the BACnet
service is crash-looping on a bad config -- that is the point of it. Stdlib only, no
bacpypes3, no internet. Plain HTTP: this is a LAN appliance.

Pages: / (status, auto-refresh), /settings, /password, /login, and GET /api/status.

Applying settings never needs root:
  1. the form is round-tripped through config.validate (load_config + stricter checks);
  2. the previous config.toml is copied to config.toml.bak-<UTC> (last 10 kept);
  3. the new file is written atomically (tmp + fsync + rename + fsync dir);
  4. the main service's watcher sees the change and exits 0; Restart=always restarts it;
  5. we wait for its heartbeat (/run/water-bacnet/main-status.json) to show it running
     on the new file for a while. Then the new file becomes config.toml.last-good.
     If it fails, exits, or falls back to last-good instead, we write last-good back
     (the rejected file is kept as config.toml.rejected) and show the error.
An apply interrupted by a portal restart is resumed from a marker file.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import getpass
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .config import (
    DEFAULT_CONFIG_PATH, Config, PortalConfig, _SECTIONS, dump_toml, load_config, loads_config,
    meter_id_problem, validate,
)
from .meter import GALLONS_PER_CUFT, Reading, _number, parse_iso

log = logging.getLogger("water_bacnet.portal")

UNIT = "water-bacnet"
SERVICE_USER = "water-bacnet"
DEFAULT_RUNTIME_DIR = "/run/water-bacnet"
STATUS_FILE = "main-status.json"  # written by water_bacnet.__main__.Supervisor

MAX_BODY = 64 * 1024
MAX_CONCURRENT = 16  # simultaneous connections; more are dropped
REQUEST_TIMEOUT_S = 20
SESSION_IDLE_S = 2 * 3600
SESSION_MAX_S = 24 * 3600
MAX_SESSIONS = 32
LOGIN_WINDOW_S = 300
LOGIN_MAX_PER_IP = 5
LOGIN_MAX_GLOBAL = 20
MAX_TRACKED_IPS = 256
PBKDF2_ITERATIONS = 260_000
MIN_PASSWORD = 8
BACKUPS_KEEP = 10
# Watcher poll + RestartSec=10 + venv check + up to 60 s waiting for the bind address
# + 15 s for the sockets (+ the 80 s fallback below, for an older main).
APPLY_TIMEOUT_S = 150.0
# A heartbeat without "ready_via" comes from a main that predates the explicit readiness
# signal (bacnet.serve -> Supervisor.mark_ready); treat it as ready once it has been
# running this long (longer than its bind wait + socket check).
LEGACY_READY_FALLBACK_S = 80.0
HEALTHY_AFTER_S = 15.0  # main must keep serving BACnet this long on the new config
HEARTBEAT_MAX_AGE_S = 20.0
CACHE_S = 5.0
REFRESH_S = 15


# ---------------------------------------------------------------------------------
# files

def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """tmp + fsync + rename + fsync(dir). The old file is intact until the rename."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _f(v) -> float:
    """A float from untrusted JSON (the heartbeat file), 0.0 if it is not one."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f else 0.0


def _read_bytes(path: Path) -> Optional[bytes]:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


# ---------------------------------------------------------------------------------
# passwords

def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS,
                  salt: Optional[bytes] = None) -> str:
    salt = salt if salt is not None else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return f"pbkdf2_sha256${iterations}${b64(salt)}${b64(dk)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = encoded.strip().split("$")
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iters)
        if not 1000 <= iterations <= 10_000_000:
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        want = base64.b64decode(hash_b64, validate=True)
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(got, want)
    except (ValueError, TypeError, UnicodeError):
        return False


def write_password_file(path: Path, password: str) -> None:
    path = Path(path)
    atomic_write(path, (hash_password(password) + "\n").encode("ascii"), mode=0o600)
    os.chmod(path, 0o600)
    if os.geteuid() == 0:  # run as root (recovery): hand it to the service user
        try:
            import pwd
            pw = pwd.getpwnam(SERVICE_USER)
            os.chown(path, pw.pw_uid, pw.pw_gid)
        except (KeyError, OSError):
            pass


# ---------------------------------------------------------------------------------
# sessions and login rate limiting

@dataclasses.dataclass
class Session:
    created: float
    last: float
    csrf: str


class SessionStore:
    """Keyed by sha256(token) so lookups do not compare secrets byte by byte."""

    def __init__(self, idle_s=SESSION_IDLE_S, max_s=SESSION_MAX_S, cap=MAX_SESSIONS):
        self.idle_s, self.max_s, self.cap = idle_s, max_s, cap
        self._d = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()

    def create(self):
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._prune(now)
            while len(self._d) >= self.cap:
                oldest = min(self._d, key=lambda k: self._d[k].last)
                del self._d[oldest]
            s = Session(now, now, secrets.token_urlsafe(32))
            self._d[self._key(token)] = s
        return token, s

    def get(self, token: Optional[str]) -> Optional[Session]:
        if not token or len(token) > 200:
            return None
        now = time.time()
        with self._lock:
            k = self._key(token)
            s = self._d.get(k)
            if s is None:
                return None
            if now - s.last > self.idle_s or now - s.created > self.max_s:
                del self._d[k]
                return None
            s.last = now
            return s

    def drop(self, token: Optional[str]) -> None:
        if token:
            with self._lock:
                self._d.pop(self._key(token), None)

    def drop_all(self) -> None:
        with self._lock:
            self._d.clear()

    def prune(self) -> None:
        with self._lock:
            self._prune(time.time())

    def _prune(self, now: float) -> None:
        for k in [k for k, s in self._d.items()
                  if now - s.last > self.idle_s or now - s.created > self.max_s]:
            del self._d[k]

    def __len__(self):
        return len(self._d)


class LoginLimiter:
    """At most LOGIN_MAX_PER_IP failures per client and LOGIN_MAX_GLOBAL overall per
    LOGIN_WINDOW_S. Bounded memory."""

    def __init__(self, window_s=LOGIN_WINDOW_S, per_ip=LOGIN_MAX_PER_IP,
                 global_max=LOGIN_MAX_GLOBAL, max_ips=MAX_TRACKED_IPS):
        self.window_s, self.per_ip, self.global_max, self.max_ips = \
            window_s, per_ip, global_max, max_ips
        self._ip = {}
        self._all = []
        self._lock = threading.Lock()

    def _trim(self, now):
        cut = now - self.window_s
        self._all = [t for t in self._all if t > cut][-self.global_max:]
        for ip in list(self._ip):
            self._ip[ip] = [t for t in self._ip[ip] if t > cut]
            if not self._ip[ip]:
                del self._ip[ip]

    def retry_after(self, ip: str) -> float:
        """Seconds until this client may try again (0 = allowed now)."""
        now = time.time()
        with self._lock:
            self._trim(now)
            waits = [0.0]
            mine = self._ip.get(ip, [])
            if len(mine) >= self.per_ip:
                waits.append(mine[0] + self.window_s - now)
            if len(self._all) >= self.global_max:
                waits.append(self._all[0] + self.window_s - now)
            return max(waits)

    def failed(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            self._trim(now)
            if ip not in self._ip and len(self._ip) >= self.max_ips:
                self._ip.pop(next(iter(self._ip)))
            self._ip.setdefault(ip, []).append(now)
            self._ip[ip] = self._ip[ip][-self.per_ip:]
            self._all.append(now)

    def succeeded(self, ip: str) -> None:
        with self._lock:
            self._ip.pop(ip, None)


# ---------------------------------------------------------------------------------
# reading the meter state (portal side; mirrors sources.StateFileSource.latest)

def state_path_for(cfg: Config) -> Path:
    if cfg.source.mode == "state_file":
        return Path(cfg.source.state_file)
    return Path(cfg.storage.state_dir) / "state.json"


def read_reading(cfg: Config, now: Optional[float] = None) -> Reading:
    """The current Reading from the state file. Never raises."""
    now = time.time() if now is None else now
    path = state_path_for(cfg)
    try:
        d = json.loads(path.read_text())
        if not isinstance(d, dict):
            raise ValueError("state file is not a JSON object")
        updated = parse_iso(d["updated"])
    except FileNotFoundError:
        return Reading.empty(f"state file missing ({path})")
    except OSError as exc:
        return Reading.empty(f"state file unreadable: {exc.strerror or exc}")
    except (ValueError, KeyError, TypeError) as exc:
        return Reading.empty(f"state file invalid: {exc}")
    file_age = now - updated
    ssp = _number(d.get("seconds_since_packet"))
    age = None if ssp is None else ssp + max(0.0, file_age)
    stale_after = cfg.meter.stale_after_s
    mid = d.get("meter_id")
    error = None
    if not cfg.meter.id:
        error = "meter id not set (Settings: Meter ORION id)"
    elif mid is not None and str(mid) != str(cfg.meter.id):
        error = f"state file is for meter {mid}, not {cfg.meter.id}"
    elif _number(d.get("volume")) is None:
        error = "no register reading yet"
    elif file_age > stale_after:
        error = f"state file not updated for {file_age:.0f}s"
    elif age is None:
        error = "no packet received yet"
    elif age > stale_after:
        error = f"no packet for {age:.0f}s"

    def as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    return Reading(
        volume_cuft=_number(d.get("volume")),
        flow_per_hour=_number(d.get("flow_per_hour")),
        flow_recent_per_hour=_number(d.get("flow_recent_per_hour")),
        seconds_since_packet=age,
        snr=_number(d.get("snr")),
        rssi=_number(d.get("rssi")),
        freq=_number(d.get("freq")),
        packets_per_hour=_number(d.get("packets_per_hour")),
        rtl433_restarts=as_int(d.get("rtl433_restarts")),
        packets_total=as_int(d.get("packets_total")),
        stale=error is not None,
        error=error,
        updated=updated,
    )


# ---------------------------------------------------------------------------------
# host facts

def host_ip() -> Optional[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET; nothing is sent
            return s.getsockname()[0]
    except OSError:
        return None


def host_uptime() -> Optional[float]:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def disk_free(path: str) -> Optional[dict]:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        u = shutil.disk_usage(p)
        return {"path": str(p), "free_bytes": u.free, "total_bytes": u.total}
    except OSError:
        return None


# ---------------------------------------------------------------------------------
# the settings form: one field per config key, generated from the dataclasses

LABELS = {
    "meter.id": ("Meter ORION id", "required; on-air id = stamped endpoint serial & 0xFFFFFF"),
    "meter.stale_after_s": ("Stale after (seconds)", "Feed Stale when the last packet is older"),
    "source.mode": ("Source mode", "rtl_433 = SDR on this Pi; state_file = follow a collector"),
    "source.state_file": ("State file (state_file mode)", "collector's state.json"),
    "source.rtl433_path": ("rtl_433 program", ""),
    "source.device": ("rtl_433 device (-d)", '":serial", or empty for the first dongle'),
    "source.frequency": ("Frequency (-f)", "e.g. 916.45M"),
    "source.sample_rate": ("Sample rate (-s)", "e.g. 1600k"),
    "source.protocol": ("Decoder protocol (-R)", "223 in rtl_433 25.02, 219 in older builds"),
    "source.restart_if_silent_s": ("Restart rtl_433 if silent (seconds)", ""),
    "source.extra_args": ("Extra rtl_433 arguments", "shell-style, e.g. -g 40"),
    "storage.state_dir": ("State directory", ""),
    "storage.raw_log": ("Keep raw packet log", "raw/YYYY-MM-DD.jsonl"),
    "bacnet.bind": ("Bind address", '"auto" or this Pi\'s address/prefix, e.g. 192.0.2.10/24'),
    "bacnet.port": ("BACnet UDP port", ""),
    "bacnet.device_instance": ("Device instance", ""),
    "bacnet.device_name": ("Device name", ""),
    "bacnet.vendor_id": ("Vendor id", ""),
    "bacnet.refresh_s": ("Refresh (seconds)", "how often values are pushed to BACnet"),
    "bacnet.model_name": ("Model name", ""),
    "bacnet.description": ("Description", ""),
    "bacnet.location": ("Location", "BACnet device location, free text (optional)"),
    "bacnet.object_names": ("Object name overrides", 'one per line: analogInput:1 = Name'),
    "portal.listen": ("Portal listen address", "0.0.0.0 = all interfaces"),
    "portal.port": ("Portal TCP port", "the portal restarts itself after a change"),
    "portal.public_status": ("Status page without login", "read-only status for anyone on the LAN"),
    "storage.raw_retention_days": ("Keep raw packet log for (days)", "older files are deleted daily"),
    "storage.save_interval_s": ("Save state every (seconds)", "fewer SD-card writes; also saved on stop"),
    "recovery.usb_reset": ("Step 2: USB reset of the dongle", "USBDEVFS_RESET, by the root helper"),
    "recovery.power_cycle": ("Step 3: USB power cycle", "uhubctl cuts the dongle's 5 V (remote replug)"),
    "recovery.uhubctl_path": ("uhubctl program", ""),
    "recovery.hub_location": ("uhubctl hub location (-l)", "Pi 4: 1-1 (all four ports switch together)"),
    "recovery.hub_port": ("uhubctl port (-p)", "empty = every port of that hub"),
    "recovery.cycle_delay_s": ("Power-off time (seconds)", "1-60"),
    "recovery.reboot": ("Step 4: reboot the Pi", "last resort; off by default"),
    "recovery.usb_vid_pid": ("Dongle USB id", "vendor:product, e.g. 0bda:2838"),
    "recovery.max_power_cycles_per_day": ("Max power cycles per day", "0-48, enforced by the root helper"),
    "recovery.max_reboots_per_week": ("Max reboots per week", "0-7, enforced by the root helper"),
    "recovery.step_timeout_s": ("Helper response timeout (seconds)", ""),
    "health.cpu_temp": ("Report CPU temperature (AI 12)", ""),
    "health.thermal_path": ("CPU temperature file", "sysfs, millidegrees C"),
    "health.throttled": ("Report throttle flags (AI 13, BI 2)", "vcgencmd get_throttled"),
    "health.vcgencmd_path": ("vcgencmd program", ""),
    "health.enclosure_sensor": ("Enclosure sensor (AI 14/15)", "auto probes SHT31 0x44, BME280 0x76/0x77"),
    "health.i2c_bus": ("I2C bus", "/dev/i2c-N; 1 on a Pi"),
    "health.i2c_address": ("I2C address", "0 = probe; or 0x44, 0x45, 0x76, 0x77"),
    "health.poll_s": ("Health poll interval (seconds)", ""),
}
CHOICES = {"source.mode": ("rtl_433", "state_file"),
           "health.enclosure_sensor": ("auto", "sht31", "bme280", "none")}
# int fields that also accept 0x.. hex in the form
HEX_FIELDS = ("health.i2c_address",)
SECTION_TITLES = {"meter": "Meter", "source": "Data source", "storage": "Storage",
                  "bacnet": "BACnet", "portal": "Web portal",
                  "recovery": "Dongle recovery (rtl_433 mode)", "health": "Box health"}


def form_fields():
    """[(section, key, default)] for every config key, in file order."""
    out = []
    for section, cls in _SECTIONS.items():
        d = cls()
        for f in dataclasses.fields(cls):
            out.append((section, f.name, getattr(d, f.name)))
    return out


def _fmt_number(v) -> str:
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    return str(v)


def form_values(cfg: Config) -> dict:
    """Config -> {"section.key": text} as the form shows it."""
    out = {}
    for section, key, default in form_fields():
        v = getattr(getattr(cfg, section), key)
        name = f"{section}.{key}"
        if isinstance(default, bool):
            out[name] = "on" if v else ""
        elif name in HEX_FIELDS and isinstance(v, int) and v:
            out[name] = f"0x{v:02x}"
        elif isinstance(default, (int, float)):
            out[name] = _fmt_number(v)
        elif isinstance(default, tuple):
            out[name] = shlex.join(v)
        elif isinstance(default, dict):
            out[name] = "\n".join(f"{k} = {val}" for k, val in v.items())
        else:
            out[name] = str(v)
    return out


def parse_form(form: dict):
    """{"section.key": text} -> (to_dict()-shaped dict, [errors])."""
    data = {s: {} for s in _SECTIONS}
    errors = []
    for section, key, default in form_fields():
        name = f"{section}.{key}"
        label = LABELS.get(name, (name, ""))[0]
        raw = form.get(name)
        if isinstance(default, bool):
            data[section][key] = raw is not None and raw != ""
            continue
        if raw is None:
            errors.append(f"{label}: missing")
            continue
        text = raw.strip()
        try:
            if isinstance(default, int):
                data[section][key] = int(text, 0 if name in HEX_FIELDS else 10)
            elif isinstance(default, float):
                v = float(text)
                if v != v or v in (float("inf"), float("-inf")):
                    raise ValueError
                data[section][key] = v
            elif isinstance(default, tuple):
                data[section][key] = shlex.split(raw)
            elif isinstance(default, dict):
                table = {}
                for line in raw.splitlines():
                    if not line.strip():
                        continue
                    k, sep, v = line.partition("=")
                    if not sep or not k.strip():
                        raise ValueError(f'"{line.strip()}" is not "key = value"')
                    table[k.strip().strip('"')] = v.strip().strip('"')
                data[section][key] = table
            else:
                if name in CHOICES and text not in CHOICES[name]:
                    raise ValueError(f"must be one of {', '.join(CHOICES[name])}")
                data[section][key] = text
        except ValueError as exc:
            if isinstance(default, int):
                errors.append(f"{label}: must be a whole number, got {text!r}")
            elif isinstance(default, float):
                errors.append(f"{label}: must be a number, got {text!r}")
            else:
                errors.append(f"{label}: {exc or 'invalid value'}")
    return data, errors


# ---------------------------------------------------------------------------------
# the portal

def main_ready_at(st: dict, now: Optional[float] = None) -> Optional[float]:
    """When main started serving BACnet, from its heartbeat; None = not (yet) serving.

    A current main says so explicitly ("ready_via": "signal", set when bacnet.serve()
    has bound its sockets). An older main has no such key: fall back to "running for
    LEGACY_READY_FALLBACK_S" unless it already reported ready_at itself.
    """
    ready_at = _f(st.get("ready_at"))
    if ready_at:
        return ready_at
    if st.get("ready_via"):
        return None
    now = time.time() if now is None else now
    started = _f(st.get("started"))
    if started and st.get("state") == "running" and now - started >= LEGACY_READY_FALLBACK_S:
        return started + LEGACY_READY_FALLBACK_S
    return None


class Portal:
    def __init__(self, config_path=DEFAULT_CONFIG_PATH, runtime_dir: Optional[str] = None,
                 use_systemd: bool = True, apply_timeout_s: float = APPLY_TIMEOUT_S,
                 healthy_after_s: float = HEALTHY_AFTER_S,
                 on_portal_change: Optional[Callable[[], None]] = None):
        self.config_path = Path(config_path)
        self.etc = self.config_path.parent
        self.last_good_path = Path(str(self.config_path) + ".last-good")
        self.rejected_path = Path(str(self.config_path) + ".rejected")
        self.pending_path = self.etc / ".portal-apply-pending.json"
        self.passwd_path = self.etc / "portal.passwd"
        self.runtime_dir = Path(runtime_dir or os.environ.get("WATER_BACNET_RUNTIME_DIR")
                                or DEFAULT_RUNTIME_DIR)
        self.use_systemd = use_systemd
        self.apply_timeout_s = apply_timeout_s
        self.healthy_after_s = healthy_after_s
        self.on_portal_change = on_portal_change
        self.sessions = SessionStore()
        self.limiter = LoginLimiter()
        self.started = time.time()
        self._apply_lock = threading.Lock()
        self._apply_thread: Optional[threading.Thread] = None
        self.apply_state = {"state": "idle"}
        self._pw_sem = threading.BoundedSemaphore(1)  # one PBKDF2 at a time
        self._cache = {}
        self._cache_lock = threading.Lock()

    # ---- config --------------------------------------------------------------

    def current_config(self):
        """-> (cfg, loaded_from "config"|"last-good"|"defaults", error). Never raises."""
        error = None
        for path, label in ((self.config_path, "config"), (self.last_good_path, "last-good")):
            try:
                return load_config(path), label, error
            except FileNotFoundError:
                if error is None:
                    error = f"{path} does not exist"
            except (OSError, ValueError) as exc:
                if error is None:
                    error = str(exc)
        return load_config(None), "defaults", error

    def portal_settings(self) -> PortalConfig:
        return self.current_config()[0].portal

    # ---- system facts (cached: phones auto-refresh) ---------------------------

    def _cached(self, key, fn):
        now = time.monotonic()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < CACHE_S:
                return hit[1]
        value = fn()
        with self._cache_lock:
            self._cache[key] = (now, value)
        return value

    def _run(self, argv, timeout=5.0):
        try:
            p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=timeout, text=True, errors="replace")
            return p.returncode, p.stdout, p.stderr
        except (OSError, subprocess.SubprocessError) as exc:
            return None, "", str(exc)

    def service_state(self, fresh: bool = False) -> str:
        if not self.use_systemd:
            return "unknown"

        def get():
            rc, out, err = self._run(["systemctl", "is-active", UNIT])
            return (out.strip().splitlines() or ["unknown"])[0] if rc is not None else "unknown"
        return get() if fresh else self._cached("active", get)

    def journal(self, lines: int = 50):
        """-> (list of lines, None) or (None, reason)."""
        if not self.use_systemd:
            return None, "journal not available"

        def get():
            rc, out, err = self._run(["journalctl", "-u", UNIT, "-n", str(lines), "--no-pager",
                                      "-q", "-o", "short-iso"])
            if rc is None:
                return None, f"journalctl not available ({err})"
            text = out.strip()
            if rc != 0 or not text:
                return None, (err.strip().splitlines() or ["journal not readable "
                                                           "(is the user in systemd-journal?)"])[0]
            return text.splitlines()[-lines:], None
        return self._cached(("journal", lines), get)

    def main_status(self) -> Optional[dict]:
        try:
            d = json.loads((self.runtime_dir / STATUS_FILE).read_text())
            return d if isinstance(d, dict) else None
        except (OSError, ValueError):
            return None

    def has_password(self) -> bool:
        data = _read_bytes(self.passwd_path)
        return bool(data and data.strip())

    def check_password(self, password: str) -> bool:
        data = _read_bytes(self.passwd_path)
        if not data or len(password) > 1024:
            return False
        with self._pw_sem:
            return verify_password(password, data.decode("ascii", "replace"))

    # ---- status ---------------------------------------------------------------

    def status(self) -> dict:
        now = time.time()
        cfg, loaded_from, cfg_error = self.current_config()
        r = read_reading(cfg, now)
        ms = self.main_status()
        main = {"active": self.service_state(), "heartbeat": None}
        if ms:
            hb_age = now - _f(ms.get("heartbeat"))
            main["heartbeat"] = {
                "state": ms.get("state"), "age_s": round(hb_age, 1),
                "alive": ms.get("state") == "running" and hb_age <= HEARTBEAT_MAX_AGE_S,
                "ready": main_ready_at(ms, now) is not None,
                "ready_via": ms.get("ready_via") or "legacy",
                "bacnet_address": ms.get("bacnet_address"),
                "health": ms.get("health") if isinstance(ms.get("health"), dict) else None,
                "running_for_s": round(now - (_f(ms.get("started")) or now), 1),
                "loaded_from": ms.get("loaded_from"), "config_error": ms.get("config_error"),
                "error": ms.get("error"), "version": ms.get("version"),
            }
        reading = dataclasses.asdict(r)
        reading["volume_gal"] = (None if r.volume_cuft is None
                                 else round(r.volume_cuft * GALLONS_PER_CUFT, 3))
        apply = {k: v for k, v in self.apply_state.items() if k != "journal"}
        return {
            "version": __version__,
            "time": now,
            "feed": "stale" if r.stale else "fresh",
            "reading": reading,
            "source": {"mode": cfg.source.mode, "state_path": str(state_path_for(cfg))},
            "bacnet": {"device_instance": cfg.bacnet.device_instance,
                       "device_name": cfg.bacnet.device_name, "bind": cfg.bacnet.bind,
                       "port": cfg.bacnet.port},
            "main_service": main,
            "config": {"path": str(self.config_path), "loaded_from": loaded_from,
                       "error": cfg_error, "last_good_exists": self.last_good_path.exists(),
                       "meter_id": cfg.meter.id, "meter_id_problem": meter_id_problem(cfg)},
            "host": {"hostname": socket.gethostname(), "ip": host_ip(),
                     "uptime_s": host_uptime(), "disk": disk_free(cfg.storage.state_dir),
                     "portal_uptime_s": round(now - self.started, 1)},
            "apply": apply,
        }

    # ---- applying a config ------------------------------------------------------

    def apply_busy(self) -> bool:
        t = self._apply_thread
        return t is not None and t.is_alive()

    def save(self, cfg: Config) -> dict:
        """Validate, back up, write, and verify (in the background). Returns apply_state."""
        text = dump_toml(cfg, header="water-bacnet configuration, written by the web portal "
                                     f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
                                     "Comments of the previous file are in config.toml.bak-*")
        validate(text)  # belt and braces: what we write must load
        data = text.encode("utf-8")
        with self._apply_lock:
            if self.apply_busy():
                raise RuntimeError("another change is still being applied; wait for it")
            old = _read_bytes(self.config_path)
            if old is not None and self._same_config(old, cfg):
                self.apply_state = {"state": "unchanged", "message": "No changes to save.",
                                    "finished": time.time()}
                return self.apply_state
            if not self.last_good_path.exists() and old is not None:
                # Seed last-good with the old file if it at least loads.
                try:
                    load_config(self.config_path)
                    atomic_write(self.last_good_path, old)
                except (OSError, ValueError):
                    pass
            if old is not None:
                self._backup(old)
            old_portal = self.current_config()[0].portal
            verify = self.service_state(fresh=True) != "inactive"
            t0 = time.time()
            marker = {"sha256": _sha(data), "started": t0, "verify": verify,
                      "portal_changed": old_portal != cfg.portal}
            atomic_write(self.pending_path, json.dumps(marker).encode())
            atomic_write(self.config_path, data)
            log.info("saved new config %s (sha256 %s)", self.config_path, marker["sha256"][:12])
            self.apply_state = {"state": "pending", "started": t0, "sha256": marker["sha256"],
                                "message": "Saved. Waiting for the BACnet service to restart "
                                           "on the new settings..."}
            self._start_verify(marker)
            return self.apply_state

    def _same_config(self, old: bytes, cfg: Config) -> bool:
        try:
            return dataclasses.replace(loads_config(old.decode("utf-8")), path=None) == \
                dataclasses.replace(cfg, path=None)
        except (ValueError, UnicodeError):
            return False

    def _backup(self, old: bytes) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = Path(f"{self.config_path}.bak-{stamp}")
        n = 1
        while path.exists():
            path = Path(f"{self.config_path}.bak-{stamp}-{n}")
            n += 1
        atomic_write(path, old)
        backups = sorted(self.etc.glob(self.config_path.name + ".bak-*"),
                         key=lambda p: (p.stat().st_mtime, p.name))
        for p in backups[:-BACKUPS_KEEP]:
            try:
                p.unlink()
            except OSError:
                pass

    def resume_pending(self) -> None:
        """A portal restart in the middle of an apply: finish verifying it."""
        try:
            marker = json.loads(self.pending_path.read_text())
            float(marker["started"])
            str(marker["sha256"])
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError):
            self._clear_marker()
            return
        data = _read_bytes(self.config_path)
        if data is None or _sha(data) != marker["sha256"]:
            self._clear_marker()  # the file changed since; nothing to verify
            return
        log.warning("resuming verification of an interrupted config apply")
        marker["started"] = min(float(marker["started"]), time.time())
        self.apply_state = {"state": "pending", "started": marker["started"],
                            "sha256": marker["sha256"],
                            "message": "Resuming check of the last saved settings..."}
        with self._apply_lock:
            self._start_verify(marker, resumed=True)

    def _start_verify(self, marker: dict, resumed: bool = False) -> None:
        t = threading.Thread(target=self._verify_guarded, args=(marker, resumed),
                             name="apply-verify", daemon=True)
        self._apply_thread = t
        t.start()

    def _verify_guarded(self, marker, resumed):
        try:
            self._verify(marker, resumed)
        except Exception as exc:  # never leave a pending apply without a verdict
            log.exception("config apply check crashed")
            self.apply_state = {"state": "error", "message": f"apply check failed: {exc!r}",
                                "finished": time.time()}
        finally:
            self._clear_marker()
        if marker.get("portal_changed") and self.on_portal_change is not None:
            try:
                self.on_portal_change()
            except Exception:
                log.exception("portal restart hook failed")

    def _clear_marker(self):
        try:
            self.pending_path.unlink()
        except OSError:
            pass

    def _verify(self, marker: dict, resumed: bool) -> None:
        sha, t0 = marker["sha256"], float(marker["started"])
        if not marker.get("verify", True):
            self.apply_state = {"state": "saved_unverified", "finished": time.time(),
                                "message": "Saved. The BACnet service is stopped, so the new "
                                           "settings were not tested; start it to use them."}
            return
        deadline = time.time() + (self.apply_timeout_s * (1.5 if resumed else 1.0))
        ok, why = self.wait_healthy(sha, t0, deadline)
        if ok:
            atomic_write(self.last_good_path, _read_bytes(self.config_path) or b"")
            self.apply_state = {"state": "applied", "finished": time.time(), "sha256": sha,
                                "message": "Applied. The BACnet service is running on the new "
                                           "settings (now also saved as last-good)."}
            log.info("config %s confirmed healthy; saved as last-good", sha[:12])
            return
        self._rollback(sha, why)

    def wait_healthy(self, sha: str, t0: float, deadline: float):
        """Wait for main to run on config `sha` for healthy_after_s. -> (ok, reason)."""
        last = None
        while True:
            now = time.time()
            st = self.main_status()
            if st and _f(st.get("started")) >= t0 - 1.0:
                last = st
                same = st.get("config_sha256") == sha
                if st.get("loaded_from") == "last-good":
                    return False, ("the BACnet service could not load the new config and fell "
                                   f"back to last-good: {st.get('config_error')}")
                if same and st.get("state") == "failed":
                    return False, f"the BACnet service failed: {st.get('error') or 'exited'}"
                ready_at = main_ready_at(st, now)
                if (same and st.get("state") == "running" and ready_at
                        and now - _f(st.get("heartbeat")) <= HEARTBEAT_MAX_AGE_S
                        and now - ready_at >= self.healthy_after_s):
                    return True, None
            if now >= deadline:
                break
            time.sleep(min(1.0, max(0.05, self.healthy_after_s / 4)))
        reason = (f"the BACnet service did not report healthy on the new settings within "
                  f"{self.apply_timeout_s:.0f}s (service state: {self.service_state(fresh=True)})")
        if last and last.get("error"):
            reason += f"; last error: {last['error']}"
        return False, reason

    def _rollback(self, sha: str, why: str) -> None:
        new = _read_bytes(self.config_path)
        good = _read_bytes(self.last_good_path)
        journal, _ = self.journal(30)
        if new is not None and _sha(new) == sha:
            atomic_write(self.rejected_path, new)
        if good is None:
            log.error("new config failed (%s) and there is no last-good to roll back to", why)
            self.apply_state = {"state": "failed", "finished": time.time(), "error": why,
                                "journal": journal,
                                "message": "The new settings did not work and there is no "
                                           "last-good config to go back to."}
            return
        atomic_write(self.config_path, good)
        log.error("new config failed (%s); rolled back to %s", why, self.last_good_path)
        self.apply_state = {"state": "rolled_back", "finished": time.time(), "error": why,
                            "journal": journal,
                            "message": "The new settings did not work, so the last-good config "
                                       "was put back (yours is kept as config.toml.rejected)."}

    def wait_apply(self, timeout: float = 30.0) -> dict:
        t = self._apply_thread
        if t is not None:
            t.join(timeout)
        return self.apply_state

    def maintenance(self) -> None:
        """Periodic: prune sessions; adopt a long-running hand-edited config as last-good."""
        self.sessions.prune()
        if self.apply_busy() or self.pending_path.exists():
            return
        st = self.main_status()
        data = _read_bytes(self.config_path)
        if not st or data is None or st.get("loaded_from") != "config":
            return
        alive = (st.get("state") == "running"
                 and time.time() - _f(st.get("heartbeat")) <= HEARTBEAT_MAX_AGE_S)
        steady = _f(st.get("heartbeat")) - _f(st.get("started")) >= 300
        if alive and steady and st.get("config_sha256") == _sha(data):
            if _read_bytes(self.last_good_path) != data:
                try:
                    load_config(self.config_path)
                    atomic_write(self.last_good_path, data)
                    log.info("config.toml has run healthy for 5 min; saved as last-good")
                except (OSError, ValueError) as exc:
                    log.warning("cannot update last-good: %s", exc)


# ---------------------------------------------------------------------------------
# HTML

CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#5d5d58;--card:#fff;--line:#d9d9d3;
--accent:#0b5cad;--ok:#1d7a3a;--okbg:#e3f3e7;--bad:#a8261b;--badbg:#fbe5e2;
--warn:#8a5a00;--warnbg:#fdf0d5;--code:#f0f0ec}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ececea;--muted:#a3a39d;
--card:#1e1e1c;--line:#34342f;--accent:#6aa9ec;--ok:#6fcf8a;--okbg:#17301f;--bad:#ff8c80;
--badbg:#3a1a17;--warn:#f0c060;--warnbg:#352a12;--code:#262623}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,-apple-system,
"Segoe UI",Roboto,sans-serif}
header{background:var(--card);border-bottom:1px solid var(--line)}
.wrap{max-width:860px;margin:0 auto;padding:12px 16px}
header .wrap{display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center}
header strong{font-size:1.05rem;margin-right:auto}
nav a,nav button{color:var(--accent);text-decoration:none;background:none;border:0;
font:inherit;padding:6px 2px;cursor:pointer}
nav a[aria-current]{font-weight:700;text-decoration:underline}
nav form{display:inline}
h1{font-size:1.4rem;margin:18px 0 10px}
h2{font-size:1.1rem;margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;
margin:0 0 14px}
.banner{border-radius:8px;padding:12px 16px;margin:0 0 14px;font-weight:600}
.ok{background:var(--okbg);color:var(--ok)}.bad{background:var(--badbg);color:var(--bad)}
.warn{background:var(--warnbg);color:var(--warn)}
.big{font-size:2rem;font-weight:700;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:6px 4px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:500;color:var(--muted);width:45%}
td{font-variant-numeric:tabular-nums;word-break:break-word}
pre{background:var(--code);padding:10px;border-radius:6px;overflow-x:auto;font-size:.8rem;
white-space:pre;margin:0}
fieldset{border:1px solid var(--line);border-radius:8px;margin:0 0 14px;padding:10px 16px;
background:var(--card)}
legend{font-weight:700;padding:0 6px}
label{display:block;font-weight:600;margin:10px 0 2px}
.help{display:block;color:var(--muted);font-size:.85rem;font-weight:400}
input[type=text],input[type=password],input[type=number],select,textarea{width:100%;
font:inherit;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);
color:var(--fg)}
input[type=checkbox]{width:1.2rem;height:1.2rem;vertical-align:middle;margin-right:6px}
.check label{display:inline}
input:focus,select:focus,textarea:focus,button:focus,a:focus{outline:2px solid var(--accent);
outline-offset:2px}
button.primary{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:10px 18px;
font:inherit;font-weight:600;cursor:pointer}
@media (prefers-color-scheme:dark){button.primary{color:#111}}
ul.errors{margin:0;padding-left:20px}
.muted{color:var(--muted);font-size:.9rem}
"""


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def fmt_age(s: Optional[float]) -> str:
    if s is None:
        return "never"
    s = int(s)
    if s < 120:
        return f"{s} s"
    if s < 7200:
        return f"{s // 60} min {s % 60} s"
    if s < 172800:
        return f"{s // 3600} h {(s % 3600) // 60} min"
    return f"{s // 86400} days {(s % 86400) // 3600} h"


def fmt_num(v, digits=1, unit="") -> str:
    if v is None:
        return "unknown"
    return f"{v:,.{digits}f}{(' ' + unit) if unit else ''}"


def fmt_bytes(n: Optional[int]) -> str:
    if n is None:
        return "unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def page(title: str, body: str, *, current: str = "", logged_in: bool = False,
         csrf: str = "", refresh: Optional[int] = None) -> bytes:
    links = [("/", "Status")]
    if logged_in:
        links += [("/settings", "Settings"), ("/password", "Password")]
    nav = "".join(
        f'<a href="{h}"{" aria-current=page" if h == current else ""}>{t}</a>' for h, t in links)
    if logged_in:
        nav += (f'<form method="post" action="/logout"><input type="hidden" name="csrf" '
                f'value="{esc(csrf)}"><button type="submit">Log out</button></form>')
    else:
        nav += f'<a href="/login"{" aria-current=page" if current == "/login" else ""}>Log in</a>'
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<meta name="color-scheme" content="light dark">{meta}'
           f'<title>{esc(title)} - Water Meter</title><style>{CSS}</style></head><body>'
           f'<header><div class="wrap"><strong>Water Meter</strong>'
           f'<nav aria-label="Main">{nav}</nav></div></header>'
           f'<main class="wrap"><h1>{esc(title)}</h1>{body}</main></body></html>')
    return doc.encode("utf-8")


def rows(pairs) -> str:
    return "<table>" + "".join(f"<tr><th scope=row>{esc(k)}</th><td>{v}</td></tr>"
                               for k, v in pairs) + "</table>"


def apply_banner(state: dict) -> str:
    s = state.get("state")
    if s in (None, "idle"):
        return ""
    cls = {"applied": "ok", "unchanged": "ok", "pending": "warn",
           "saved_unverified": "warn"}.get(s, "bad")
    out = f'<div class="banner {cls}" role="status">{esc(state.get("message"))}'
    if state.get("error"):
        out += f'<div class="muted">{esc(state["error"])}</div>'
    out += "</div>"
    if state.get("journal"):
        out += ('<div class="card"><h2>BACnet service log at the time</h2><pre>'
                + esc("\n".join(state["journal"])) + "</pre></div>")
    return out


def render_status(st: dict, journal, journal_err, logged_in: bool = True) -> str:
    r = st["reading"]
    fresh = st["feed"] == "fresh"
    out = []
    if fresh:
        out.append('<div class="banner ok" role="status">Feed OK: receiving the meter</div>')
    else:
        out.append(f'<div class="banner bad" role="alert">Feed STALE: {esc(r["error"])}</div>')
    c = st["config"]
    if c.get("meter_id_problem"):
        out.append('<div class="banner bad" role="alert">Setup needed: '
                   f'{esc(c["meter_id_problem"])}</div>')
    if c["loaded_from"] != "config":
        out.append(f'<div class="banner warn">config.toml cannot be used ({esc(c["error"])}); '
                   f'showing {esc(c["loaded_from"])} settings.</div>')
    main = st["main_service"]
    hb = main.get("heartbeat") or {}
    if hb.get("loaded_from") == "last-good":
        out.append('<div class="banner warn">The BACnet service is running on the LAST-GOOD '
                   f'config because config.toml failed to load: {esc(hb.get("config_error"))}'
                   '</div>')
    if st["apply"].get("state") not in (None, "idle", "unchanged", "applied"):
        out.append(apply_banner(st["apply"]))
    out.append('<div class="card"><h2>Meter register</h2>'
               f'<div class="big">{fmt_num(r["volume_cuft"], 0, "cu ft")}</div>'
               f'<div class="muted">{fmt_num(r["volume_gal"], 0, "US gal")}</div></div>')
    out.append('<div class="card"><h2>Feed</h2>' + rows([
        ("Last packet", esc(fmt_age(r["seconds_since_packet"])) + " ago"),
        ("Packets per hour", esc(fmt_num(r["packets_per_hour"], 1)) +
         ' <span class="muted">(~22 is normal)</span>'),
        ("Packets total", esc(r["packets_total"])),
        ("Flow, ~6 h average", esc(fmt_num(r["flow_per_hour"], 2, "cu ft/h"))),
        ("Flow, last hour", esc(fmt_num(r["flow_recent_per_hour"], 2, "cu ft/h"))),
        ("Signal SNR / RSSI", f'{esc(fmt_num(r["snr"], 1, "dB"))} / '
                              f'{esc(fmt_num(r["rssi"], 1, "dB"))}'),
        ("Frequency", esc(fmt_num(r["freq"], 3, "MHz"))),
        ("rtl_433 restarts", esc(r["rtl433_restarts"])),
        ("Data source", f'{esc(st["source"]["mode"])} '
                        f'<span class="muted">{esc(st["source"]["state_path"])}</span>'),
    ]) + "</div>")
    active = main["active"]
    alive = hb.get("alive")
    svc = esc(active)
    if hb:
        hb_state = hb.get("state")
        if hb_state == "running" and not hb.get("ready"):
            hb_state = "starting"
        elif hb_state == "running" and hb.get("bacnet_address"):
            hb_state = f"serving on {hb['bacnet_address']}"
        svc += (f' <span class="muted">(heartbeat {esc(hb_state)}, '
                f'{esc(fmt_age(hb.get("age_s")))} ago; up {esc(fmt_age(hb.get("running_for_s")))}'
                f')</span>')
    if hb.get("error") and not alive:
        svc += f'<div class="muted">last error: {esc(hb.get("error"))}</div>'
    health = hb.get("health") or {}
    b = st["bacnet"]
    h = st["host"]
    disk = h.get("disk") or {}
    out.append('<div class="card"><h2>Service and host</h2>' + rows([
        ("BACnet service (water-bacnet)", svc),
        ("BACnet device", f'{esc(b["device_instance"])} {esc(b["device_name"])}, '
                          f'UDP {esc(b["port"])}, bind {esc(b["bind"])}'),
        ("Host", f'{esc(h["hostname"])} {esc(h["ip"] or "")}'),
        ("Host uptime", esc(fmt_age(h["uptime_s"]))),
        ("Disk free", f'{esc(fmt_bytes(disk.get("free_bytes")))} of '
                      f'{esc(fmt_bytes(disk.get("total_bytes")))} '
                      f'<span class="muted">{esc(disk.get("path", ""))}</span>'),
        ("Software version", esc(st["version"])),
    ]) + "</div>")
    if health:
        thr = health.get("throttled_hex")
        thr_txt = "not available" if thr is None else (
            f'<strong>{esc(thr)} THROTTLED / UNDER-VOLTAGE NOW</strong>'
            if health.get("throttled_now") else f"{esc(thr)} (OK now)")
        sensor = health.get("enclosure_sensor")
        out.append('<div class="card"><h2>Box health</h2>' + rows([
            ("CPU temperature", esc(fmt_num(health.get("cpu_temp_c"), 1, "°C"))),
            ("Throttle flags", thr_txt),
            ("Enclosure", (f'{esc(fmt_num(health.get("enclosure_temp_c"), 1, "°C"))}, '
                           f'{esc(fmt_num(health.get("enclosure_rh"), 0, "% RH"))} '
                           f'<span class="muted">{esc(sensor)}</span>') if sensor
             else "no sensor"),
        ]) + "</div>")
    if not logged_in:
        out.append('<p class="muted">Service log: <a href="/login">log in</a> to see it.</p>')
    elif journal:
        out.append('<div class="card"><h2>Recent BACnet service log</h2><pre>'
                   + esc("\n".join(journal)) + "</pre></div>")
    else:
        out.append(f'<p class="muted">Service log: {esc(journal_err)}</p>')
    out.append(f'<p class="muted">Refreshes every {REFRESH_S} s. JSON: '
               '<a href="/api/status">/api/status</a></p>')
    return "".join(out)


def render_settings(values: dict, csrf: str, errors=(), notice: str = "") -> str:
    out = [notice]
    if errors:
        out.append('<div class="banner bad" role="alert"><p style="margin:0 0 6px">'
                   'Not saved. Please fix:</p><ul class="errors">'
                   + "".join(f"<li>{esc(e)}</li>" for e in errors) + "</ul></div>")
    out.append('<form method="post" action="/settings" novalidate>'
               f'<input type="hidden" name="csrf" value="{esc(csrf)}">')
    section = None
    for sec, key, default in form_fields():
        if sec != section:
            if section is not None:
                out.append("</fieldset>")
            out.append(f"<fieldset><legend>{esc(SECTION_TITLES.get(sec, sec))}</legend>")
            section = sec
        name = f"{sec}.{key}"
        label, helptext = LABELS.get(name, (name, ""))
        fid = "f-" + name.replace(".", "-")
        hid = f' aria-describedby="{fid}-h"' if helptext else ""
        help_html = f'<span class="help" id="{fid}-h">{esc(helptext)}</span>' if helptext else ""
        v = values.get(name, "")
        if isinstance(default, bool):
            out.append(f'<div class="check"><label style="margin-top:12px"><input type="checkbox" '
                       f'id="{fid}" name="{esc(name)}"{" checked" if v else ""}{hid}>'
                       f'{esc(label)}</label>{help_html}</div>')
            continue
        out.append(f'<label for="{fid}">{esc(label)}{help_html}</label>')
        if name in CHOICES:
            opts = "".join(f'<option{" selected" if c == v else ""}>{esc(c)}</option>'
                           for c in CHOICES[name])
            out.append(f'<select id="{fid}" name="{esc(name)}"{hid}>{opts}</select>')
        elif isinstance(default, dict):
            out.append(f'<textarea id="{fid}" name="{esc(name)}" rows="3"{hid}>{esc(v)}</textarea>')
        else:
            mode = (' inputmode="numeric"' if isinstance(default, int)
                    else ' inputmode="decimal"' if isinstance(default, float) else "")
            out.append(f'<input type="text" id="{fid}" name="{esc(name)}" value="{esc(v)}"'
                       f' autocomplete="off" spellcheck="false"{mode}{hid}>')
    out.append("</fieldset>")
    out.append('<p class="muted">Saving restarts the BACnet service (a BACnet client may miss one poll). '
               'If it does not come up healthy within about a minute, the last-good settings are '
               'restored automatically.</p><button class="primary" type="submit">Save and apply'
               '</button></form>')
    return "".join(out)


# ---------------------------------------------------------------------------------
# HTTP

class _Done(Exception):
    """The response was already sent (e.g. a redirect to /login)."""


class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class PortalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, addr, portal: Portal):
        self.portal = portal
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        super().__init__(addr, Handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)  # too many at once: drop, do not queue
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        log.debug("connection error from %s", client_address, exc_info=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "water-bacnet-portal"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = REQUEST_TIMEOUT_S

    # ---- plumbing --------------------------------------------------------------

    @property
    def portal(self) -> Portal:
        return self.server.portal

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)

    def handle(self):
        try:
            super().handle()
        except Exception:
            log.debug("request handling aborted", exc_info=True)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_HEAD(self):
        self._dispatch("GET", head=True)

    ROUTES = {
        ("GET", "/"): "get_status", ("GET", "/api/status"): "get_api_status",
        ("GET", "/login"): "get_login", ("POST", "/login"): "post_login",
        ("POST", "/logout"): "post_logout",
        ("GET", "/settings"): "get_settings", ("POST", "/settings"): "post_settings",
        ("GET", "/password"): "get_password", ("POST", "/password"): "post_password",
    }

    def _dispatch(self, method: str, head: bool = False):
        self._sent = False
        self._head = head
        self._set_cookies = []
        try:
            path = urlsplit(self.path).path
            name = self.ROUTES.get((method, path))
            if name is None:
                if any(p == path for _, p in self.ROUTES):
                    raise HTTPError(405, "Method not allowed")
                raise HTTPError(404, "Page not found")
            getattr(self, name)()
        except _Done:
            pass
        except HTTPError as e:
            self._error_page(e.status, e.message)
        except (ConnectionError, TimeoutError, socket.timeout):
            self.close_connection = True
        except Exception:
            # Escape the client-controlled path so CR/LF can't forge journal lines.
            safe_path = self.path[:200].replace("\r", "\\r").replace("\n", "\\n")
            log.exception("error handling %s %s", method, safe_path)
            self._error_page(500, "Internal error. It has been logged; the portal keeps running.")

    def _error_page(self, status: int, message: str):
        if self._sent:
            self.close_connection = True
            return
        if self.path.startswith("/api/"):
            self._send(status, json.dumps({"error": message}).encode(), "application/json")
        else:
            s, _ = self._session()
            body = page(HTTPStatus(status).phrase, f'<div class="banner bad">{esc(message)}</div>',
                        logged_in=s is not None, csrf=s.csrf if s else "")
            self._send(status, body)

    def _send(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8",
              headers=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                         "frame-ancestors 'none'; base-uri 'none'")
        for c in self._set_cookies:
            self.send_header("Set-Cookie", c)
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self._sent = True
        if not self._head:
            self.wfile.write(body)

    def _redirect(self, where: str):
        self._send(303, b"", headers=[("Location", where)])

    def _cookie(self, name: str) -> Optional[str]:
        raw = self.headers.get("Cookie")
        if not raw or len(raw) > 4096:
            return None
        try:
            c = SimpleCookie()
            c.load(raw)
        except CookieError:
            return None
        m = c.get(name)
        return m.value if m else None

    def _add_cookie(self, name: str, value: str, max_age: int):
        self._set_cookies.append(f"{name}={value}; Path=/; Max-Age={max_age}; HttpOnly; "
                                 "SameSite=Strict")

    def _session(self):
        token = self._cookie("wb_session")
        return self.portal.sessions.get(token), token

    def _require_login(self) -> Session:
        s, _ = self._session()
        if s is None:
            if self.path.startswith("/api/"):
                raise HTTPError(401, "login required")
            self._redirect("/login")
            raise _Done()
        return s

    def _read_form(self) -> dict:
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if origin and origin != "null" and host and urlsplit(origin).netloc != host:
            raise HTTPError(403, "Cross-site request refused")
        length = self.headers.get("Content-Length")
        if length is None:
            raise HTTPError(411, "Content-Length required")
        try:
            n = int(length)
        except ValueError:
            raise HTTPError(400, "Bad Content-Length")
        if n < 0 or n > MAX_BODY:
            self.close_connection = True
            raise HTTPError(413, "Request too large")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            raise HTTPError(415, "Form data expected")
        body = self.rfile.read(n)
        if len(body) != n:
            raise HTTPError(400, "Incomplete request body")
        try:
            parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True,
                              max_num_fields=200, strict_parsing=False)
        except (UnicodeDecodeError, ValueError):
            raise HTTPError(400, "Malformed form data")
        return {k: v[0] for k, v in parsed.items() if v}

    def _check_csrf(self, form: dict, expected: Optional[str]):
        got = form.get("csrf", "")
        if not expected or not hmac.compare_digest(got.encode("utf-8", "replace"),
                                                   expected.encode("utf-8", "replace")):
            raise HTTPError(403, "Form expired or invalid (CSRF check failed). Reload the page "
                                 "and try again.")

    # ---- pages -------------------------------------------------------------------

    def _status_allowed(self) -> Optional[Session]:
        s, _ = self._session()
        if s is None and not self.portal.portal_settings().public_status:
            return None
        return s or Session(0, 0, "")

    def get_status(self):
        s = self._status_allowed()
        if s is None:
            self._redirect("/login")
            return
        st = self.portal.status()
        logged_in = bool(s.csrf)
        # The journal can contain paths, addresses and errors: logged-in users only,
        # even when public_status lets anyone see the numbers.
        journal, jerr = self.portal.journal(50) if logged_in else (None, None)
        body = render_status(st, journal, jerr, logged_in)
        self._send(200, page("Status", body, current="/", logged_in=bool(s.csrf), csrf=s.csrf,
                             refresh=REFRESH_S))

    def get_api_status(self):
        if self._status_allowed() is None:
            raise HTTPError(401, "login required")
        self._send(200, json.dumps(self.portal.status(), indent=1).encode(), "application/json")

    def get_login(self, error: str = "", status: int = 200):
        if not self.portal.has_password():
            body = ('<div class="banner warn">No portal password is set. On the Pi run:<br>'
                    '<code>sudo -u water-bacnet /opt/water-bacnet/venv/bin/python -m '
                    'water_bacnet.portal --set-password</code></div>')
            self._send(200 if not error else status, page("Log in", body, current="/login"))
            return
        token = secrets.token_urlsafe(24)
        self._add_cookie("wb_login", token, 1800)
        err = f'<div class="banner bad" role="alert">{esc(error)}</div>' if error else ""
        body = (f'{err}<form method="post" action="/login" class="card">'
                f'<input type="hidden" name="csrf" value="{esc(token)}">'
                '<label for="pw">Password</label><input type="password" id="pw" name="password" '
                'autocomplete="current-password" required autofocus>'
                '<p><button class="primary" type="submit">Log in</button></p></form>'
                '<p class="muted">Forgot it? On the Pi: <code>sudo -u water-bacnet '
                '/opt/water-bacnet/venv/bin/python -m water_bacnet.portal --set-password</code>'
                '</p>')
        self._send(status, page("Log in", body, current="/login"))

    def post_login(self):
        form = self._read_form()
        self._check_csrf(form, self._cookie("wb_login"))
        ip = self.client_address[0]
        wait = self.portal.limiter.retry_after(ip)
        if wait > 0:
            self._set_cookies = []
            raise HTTPError(429, f"Too many failed logins. Try again in {int(wait) + 1} s.")
        if not self.portal.check_password(form.get("password", "")):
            self.portal.limiter.failed(ip)
            log.warning("portal: failed login from %s", ip)
            self.get_login("Wrong password.", status=401)
            return
        self.portal.limiter.succeeded(ip)
        token, _ = self.portal.sessions.create()
        log.info("portal: login from %s", ip)
        self._add_cookie("wb_session", token, SESSION_MAX_S)
        self._add_cookie("wb_login", "", 0)
        self._redirect("/settings")

    def post_logout(self):
        form = self._read_form()
        s, token = self._session()
        if s is not None:
            self._check_csrf(form, s.csrf)
            self.portal.sessions.drop(token)
        self._add_cookie("wb_session", "", 0)
        self._redirect("/")

    def get_settings(self):
        s = self._require_login()
        cfg, loaded_from, err = self.portal.current_config()
        notice = apply_banner(self.portal.apply_state)
        if loaded_from != "config":
            notice += (f'<div class="banner warn">config.toml cannot be used ({esc(err)}). The '
                       f'form shows the {esc(loaded_from)} settings; saving replaces '
                       'config.toml.</div>')
        refresh = 3 if self.portal.apply_state.get("state") == "pending" else None
        self._send(200, page("Settings", render_settings(form_values(cfg), s.csrf, notice=notice),
                             current="/settings", logged_in=True, csrf=s.csrf, refresh=refresh))

    def post_settings(self):
        s = self._require_login()
        form = self._read_form()
        self._check_csrf(form, s.csrf)
        data, errors = parse_form(form)
        cfg = None
        if not errors:
            try:
                cfg = validate(dump_toml(data))
            except ValueError as exc:
                errors = [e.strip() for e in str(exc).split("; ") if e.strip()]
        if errors:
            values = {k: v for k, v in form.items() if k != "csrf"}
            self._send(400, page("Settings", render_settings(values, s.csrf, errors),
                                 current="/settings", logged_in=True, csrf=s.csrf))
            return
        try:
            self.portal.save(cfg)
        except RuntimeError as exc:
            raise HTTPError(409, str(exc))
        except (OSError, ValueError) as exc:
            log.error("portal: cannot save config: %s", exc)
            raise HTTPError(500, f"Could not save the config: {exc}")
        self._redirect("/settings")

    def get_password(self, error: str = "", notice: str = "", status: int = 200):
        s = self._require_login()
        if not error and not notice and "changed=1" in (urlsplit(self.path).query or ""):
            notice = "Password changed. Other sessions were logged out."
        err = f'<div class="banner bad" role="alert">{esc(error)}</div>' if error else ""
        ok = f'<div class="banner ok" role="status">{esc(notice)}</div>' if notice else ""
        body = (f'{err}{ok}<form method="post" action="/password" class="card">'
                f'<input type="hidden" name="csrf" value="{esc(s.csrf)}">'
                '<label for="cur">Current password</label><input type="password" id="cur" '
                'name="current" autocomplete="current-password" required>'
                f'<label for="new">New password <span class="help">at least {MIN_PASSWORD} '
                'characters</span></label><input type="password" id="new" name="new" '
                'autocomplete="new-password" required>'
                '<label for="again">New password again</label><input type="password" id="again" '
                'name="again" autocomplete="new-password" required>'
                '<p><button class="primary" type="submit">Change password</button></p></form>')
        self._send(status, page("Change password", body, current="/password", logged_in=True,
                                csrf=s.csrf))

    def post_password(self):
        s = self._require_login()
        form = self._read_form()
        self._check_csrf(form, s.csrf)
        ip = self.client_address[0]
        if self.portal.limiter.retry_after(ip) > 0:
            raise HTTPError(429, "Too many failed attempts. Wait a few minutes.")
        new, again = form.get("new", ""), form.get("again", "")
        if not self.portal.check_password(form.get("current", "")):
            self.portal.limiter.failed(ip)
            self.get_password("The current password is wrong.", status=400)
            return
        if len(new) < MIN_PASSWORD or len(new) > 256:
            self.get_password(f"The new password must be {MIN_PASSWORD} to 256 characters.",
                              status=400)
            return
        if new != again:
            self.get_password("The two new passwords do not match.", status=400)
            return
        write_password_file(self.portal.passwd_path, new)
        self.portal.sessions.drop_all()
        token, s2 = self.portal.sessions.create()
        self._add_cookie("wb_session", token, SESSION_MAX_S)
        log.info("portal: password changed from %s; other sessions logged out", ip)
        self._redirect("/password?changed=1")


# ---------------------------------------------------------------------------------
# running

def bind_server(portal: Portal, listen: str, port: int) -> PortalServer:
    """Bind the configured address; fall back to 0.0.0.0:8080 so a bad setting never
    locks you out. Retries forever (systemd also restarts us)."""
    attempts = [(listen, port)]
    if (listen, port) != ("0.0.0.0", 8080):
        attempts.append(("0.0.0.0", 8080))
    delay = 2.0
    while True:
        for host, p in attempts:
            try:
                return PortalServer((host, p), portal)
            except OSError as exc:
                log.error("portal: cannot listen on %s:%d: %s", host, p, exc)
        time.sleep(delay)
        delay = min(delay * 2, 60.0)


def serve(portal: Portal, listen: Optional[str] = None, port: Optional[int] = None) -> int:
    pc = portal.portal_settings()
    listen = listen or os.environ.get("WATER_BACNET_PORTAL_LISTEN") or pc.listen
    try:
        port = int(port or os.environ.get("WATER_BACNET_PORTAL_PORT") or pc.port)
    except ValueError:
        port = pc.port
    server = bind_server(portal, listen, port)
    host, bound = server.server_address[:2]
    log.info("water-bacnet portal %s listening on http://%s:%d/ (config %s)",
             __version__, host, bound, portal.config_path)
    if not portal.has_password():
        log.warning("no portal password set: run python -m water_bacnet.portal --set-password")

    stop = threading.Event()

    def restart():
        log.info("portal settings changed; exiting so systemd restarts the portal")
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    if portal.on_portal_change is None:
        portal.on_portal_change = restart

    def housekeeping():
        while not stop.wait(30):
            try:
                portal.maintenance()
            except Exception:
                log.exception("portal maintenance failed")

    threading.Thread(target=housekeeping, name="maintenance", daemon=True).start()
    portal.resume_pending()

    import signal

    def on_signal(signum, frame):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, on_signal)
        except ValueError:  # not the main thread (tests)
            pass

    while not stop.is_set():
        try:
            server.serve_forever(poll_interval=0.5)
        except Exception:
            log.exception("portal server loop crashed; continuing")
            time.sleep(1)
    server.server_close()
    return 0


def _set_password_cli(portal: Portal, from_stdin: bool) -> int:
    if from_stdin:
        pw = sys.stdin.readline().rstrip("\r\n")
        again = pw
    else:
        pw = getpass.getpass("New portal password: ")
        again = getpass.getpass("Again: ")
    if pw != again:
        print("error: passwords do not match", file=sys.stderr)
        return 1
    if len(pw) < MIN_PASSWORD:
        print(f"error: at least {MIN_PASSWORD} characters", file=sys.stderr)
        return 1
    try:
        write_password_file(portal.passwd_path, pw)
    except OSError as exc:
        print(f"error: cannot write {portal.passwd_path}: {exc}", file=sys.stderr)
        return 1
    print(f"portal password written to {portal.passwd_path}")
    return 0


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="water_bacnet.portal",
                                 description="Web configuration portal for water-bacnet")
    ap.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                    help=f"config file (default {DEFAULT_CONFIG_PATH})")
    ap.add_argument("--listen", help="override [portal] listen")
    ap.add_argument("--port", type=int, help="override [portal] port")
    ap.add_argument("--set-password", action="store_true",
                    help="set the admin password (prompts) and exit")
    ap.add_argument("--password-stdin", action="store_true",
                    help="with --set-password: read the password from stdin")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    level = getattr(logging, os.environ.get("WATER_BACNET_LOG", "INFO").upper(), logging.INFO)
    logging.basicConfig(stream=sys.stderr, level=level if isinstance(level, int) else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    portal = Portal(args.config)
    if args.set_password:
        return _set_password_cli(portal, args.password_stdin)
    return serve(portal, args.listen, args.port)


if __name__ == "__main__":
    sys.exit(main())
