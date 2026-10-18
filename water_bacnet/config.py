"""Configuration: /etc/water-bacnet/config.toml parsed with stdlib tomllib.

Every key has a default, so an empty (or absent) file gives a working config.
Unknown keys are logged and ignored rather than fatal, so a typo never stops the
service from starting -- but it is visible in the journal.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import logging
import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "/etc/water-bacnet/config.toml"


@dataclass(frozen=True)
class MeterConfig:
    # On-air ORION id. REQUIRED: there is no usable default (0 = not set), and the
    # service and --check refuse to run without it. See METER_ID_HELP.
    id: int = 0
    stale_after_s: float = 900.0


@dataclass(frozen=True)
class SourceConfig:
    mode: str = "rtl_433"  # "rtl_433" | "state_file"
    state_file: str = "/var/lib/water-meter/state.json"
    rtl433_path: str = "rtl_433"
    # "" = let rtl_433 pick the first dongle. Recommended: pin your dongle by its USB
    # serial, ":<serial>" (e.g. ":00000001"; set one with rtl_eeprom -s), so a second
    # dongle or a re-enumeration never makes rtl_433 open the wrong one.
    device: str = ""
    frequency: str = "916.45M"
    sample_rate: str = "1600k"
    # Badger ORION is protocol 223 in rtl_433 25.02; older builds numbered it 219.
    protocol: int = 223
    restart_if_silent_s: float = 1800.0
    extra_args: tuple = ()  # appended verbatim to the rtl_433 command line


@dataclass(frozen=True)
class StorageConfig:
    state_dir: str = "/var/lib/water-bacnet"
    raw_log: bool = True
    # raw/YYYY-MM-DD.jsonl older than this many days are deleted (pruned daily).
    raw_retention_days: int = 365
    # state.json is rewritten at most this often (SD-card wear), and on shutdown.
    save_interval_s: float = 900.0


@dataclass(frozen=True)
class RecoveryConfig:
    """Remote recovery ladder for a wedged RTL-SDR (rtl_433 mode).

    restart -> usb_reset -> power_cycle -> reboot, one step per silent period. The
    privileged steps are performed by the root helper (water_bacnet.helper), which
    reads these values from this file itself -- never from the request.
    """
    usb_reset: bool = True
    power_cycle: bool = True
    uhubctl_path: str = "uhubctl"
    hub_location: str = "1-1"  # Pi 4: all four ports switch together
    hub_port: str = ""  # "" = all ports at hub_location
    cycle_delay_s: int = 5  # seconds of VBUS off during a power cycle
    reboot: bool = False  # last-resort step (d), off by default
    usb_vid_pid: str = "0bda:2838"
    max_power_cycles_per_day: int = 4
    max_reboots_per_week: int = 1
    step_timeout_s: float = 120.0  # how long the service waits for the helper


@dataclass(frozen=True)
class HealthConfig:
    """Health of the box itself (BACnet AI12-AI15, BI2). Every sensor is optional."""
    cpu_temp: bool = True
    thermal_path: str = "/sys/class/thermal/thermal_zone0/temp"
    throttled: bool = True
    vcgencmd_path: str = "vcgencmd"
    enclosure_sensor: str = "auto"  # auto | sht31 | bme280 | none
    i2c_bus: int = 1
    i2c_address: int = 0  # 0 = probe 0x44 (SHT31), then 0x76 / 0x77 (BME280)
    poll_s: float = 60.0


@dataclass(frozen=True)
class BacnetConfig:
    bind: str = "auto"  # "auto" or an explicit "192.0.2.10/24"
    port: int = 47808
    device_instance: int = 2801
    device_name: str = "WaterMeter"
    vendor_id: int = 999
    refresh_s: float = 15.0
    model_name: str = "Badger ORION via rtl_433"
    description: str = "Water meter (Badger ORION endpoint, read with rtl_433)"
    location: str = ""  # BACnet Device "location" property, e.g. "Plant room, east wall"
    # Optional per-object name overrides, e.g. {"analogInput:1" = "Meter Register"}.
    object_names: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PortalConfig:
    # Web configuration portal (python -m water_bacnet.portal). Plain HTTP, LAN only.
    listen: str = "0.0.0.0"
    port: int = 8080
    # Status page and /api/status viewable without logging in (read-only).
    public_status: bool = True


@dataclass(frozen=True)
class Config:
    meter: MeterConfig = field(default_factory=MeterConfig)
    source: SourceConfig = field(default_factory=SourceConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    bacnet: BacnetConfig = field(default_factory=BacnetConfig)
    portal: PortalConfig = field(default_factory=PortalConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    path: Optional[str] = None  # file it was loaded from (None = built-in defaults)

    # Convenience aliases used across modules.
    @property
    def refresh_s(self) -> float:
        return self.bacnet.refresh_s

    @property
    def stale_after_s(self) -> float:
        return self.meter.stale_after_s

    @property
    def meter_id(self) -> int:
        return self.meter.id

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["source"]["extra_args"] = list(d["source"]["extra_args"])
        return d


METER_ID_HELP = (
    "[meter] id is not set. It is the meter's on-air Badger ORION id, which must be set "
    "before the service will run. To find it, run "
    "`rtl_433 -f 916.45M -s 1600k -R 223 -F json` near the meter (protocol 219 on older "
    "rtl_433 builds) and look for a \"Badger-ORION\" packet whose \"id\" equals the low "
    "24 bits of the serial number stamped on the endpoint: serial & 0xFFFFFF (for example "
    "serial 24431537 -> id 7654321). Set it in the web portal (Settings) or in "
    "config.toml as [meter] id = <id>."
)


def meter_id_problem(cfg: "Config") -> Optional[str]:
    """None if [meter] id is usable, else METER_ID_HELP (an id of 0 means "not set").

    Deliberately NOT part of load_config: a config without an id must still load so
    the web portal can start and let the user set it.
    """
    mid = cfg.meter.id
    if not isinstance(mid, int) or isinstance(mid, bool) or not 1 <= mid <= 0xFFFFFFFF:
        return METER_ID_HELP
    return None


_SECTIONS = {
    "meter": MeterConfig,
    "source": SourceConfig,
    "storage": StorageConfig,
    "bacnet": BacnetConfig,
    "portal": PortalConfig,
    "recovery": RecoveryConfig,
    "health": HealthConfig,
}

# A bad [portal] or [health] value must never stop the BACnet service: those keys fall
# back to their defaults with a warning instead of failing the whole load. (The portal
# still refuses to SAVE them: see problems().)
_LENIENT_SECTIONS = ("portal", "health")

ENCLOSURE_SENSORS = ("auto", "sht31", "bme280", "none")


def _coerce(section: str, name: str, value: Any, default: Any) -> Any:
    """Convert a TOML value to the type of the default, with a clear error."""
    where = f"[{section}] {name}"
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{where} must be true/false, got {value!r}")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{where} must be an integer, got {value!r}")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{where} must be a number, got {value!r}")
        if not math.isfinite(float(value)):  # TOML allows inf / nan
            raise ValueError(f"{where} must be a finite number, got {value!r}")
        return float(value)
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ValueError(f"{where} must be a string, got {value!r}")
        return value
    if isinstance(default, tuple):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{where} must be a list of strings, got {value!r}")
        return tuple(value)
    if isinstance(default, dict):
        if not isinstance(value, dict):
            raise ValueError(f"{where} must be a table, got {value!r}")
        return {str(k): str(v) for k, v in value.items()}
    return value


def _build(data: dict, path: Optional[str]) -> Config:
    parts = {}
    for section, cls in _SECTIONS.items():
        raw = data.get(section, {})
        if not isinstance(raw, dict):
            raise ValueError(f"[{section}] must be a table")
        defaults = cls()
        kwargs = {}
        known = {f.name for f in dataclasses.fields(cls)}
        for key, value in raw.items():
            if key not in known:
                log.warning("config: ignoring unknown key [%s] %s", section, key)
                continue
            try:
                kwargs[key] = _coerce(section, key, value, getattr(defaults, key))
            except ValueError as exc:
                if section not in _LENIENT_SECTIONS:
                    raise
                log.warning("config: %s; using the default", exc)
        parts[section] = cls(**kwargs)
    for section in data:
        if section not in _SECTIONS:
            log.warning("config: ignoring unknown section [%s]", section)

    cfg = Config(path=path, **parts)
    _check_load(cfg)
    return cfg


def parse_bind(bind: str) -> Optional[ipaddress.IPv4Interface]:
    """[bacnet] bind -> None for "auto", else the IPv4 interface. Raises ValueError.

    A bare address (no /prefix) is accepted and means /24, as bacnet.resolve_bind does.
    """
    b = (bind or "").strip()
    if b.lower() == "auto":
        return None
    try:
        iface = ipaddress.IPv4Interface(b if "/" in b else b + "/24")
    except ValueError:
        raise ValueError(f'[bacnet] bind must be "auto" or an IPv4 address/prefix such as '
                         f'"192.0.2.10/24", got {bind!r}') from None
    net = iface.network
    not_host = net.prefixlen < 31 and iface.ip in (net.network_address, net.broadcast_address)
    if iface.ip.is_unspecified or iface.ip.is_multicast or not_host:
        raise ValueError(f"[bacnet] bind must be one of this host's own addresses, got {bind!r}")
    return iface


def _check_load(cfg: Config) -> None:
    """Checks a config must pass to be RUN (a failure falls back to last-good).

    Deliberately limited to values that would make the service misbehave; the portal
    applies the stricter problems() before it saves anything.
    """
    def need(ok: bool, msg: str) -> None:
        if not ok:
            raise ValueError(msg)

    m, s, b, r = cfg.meter, cfg.source, cfg.bacnet, cfg.recovery
    need(s.mode in ("rtl_433", "state_file"),
         f'[source] mode must be "rtl_433" or "state_file", got {s.mode!r}')
    need(0 < b.port < 65536, f"[bacnet] port out of range: {b.port}")
    need(0 <= b.device_instance <= 4194302,
         f"[bacnet] device_instance out of range: {b.device_instance}")
    need(0 <= b.vendor_id <= 65535, f"[bacnet] vendor_id must be 0-65535, got {b.vendor_id}")
    need(1 <= b.refresh_s <= 300, f"[bacnet] refresh_s must be 1-300 s, got {b.refresh_s}")
    need(60 <= m.stale_after_s <= 86400,
         f"[meter] stale_after_s must be 60-86400 s, got {m.stale_after_s}")
    need(s.restart_if_silent_s >= 300,
         f"[source] restart_if_silent_s must be at least 300 s, got {s.restart_if_silent_s}")
    need(1 <= r.cycle_delay_s <= 60, f"[recovery] cycle_delay_s must be 1-60, got {r.cycle_delay_s}")
    parse_bind(b.bind)


def load_config(path: Union[str, Path, None] = None) -> Config:
    """Load a config file. path=None returns the built-in defaults.

    A missing file raises FileNotFoundError; bad TOML or bad values raise ValueError.
    """
    if path is None:
        return _build({}, None)
    p = Path(path)
    with p.open("rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{p}: {exc}") from exc
    return _build(data, str(p))


def loads_config(text: str, path: Optional[str] = None) -> Config:
    """Parse config TOML from a string (same rules as load_config)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path or 'config'}: {exc}") from exc
    return _build(data, path)


# ---------------------------------------------------------------------------------
# Writing config (used by the web portal). The stdlib has no TOML writer; this schema
# is flat (scalars, one list of strings, one string->string table), so a small
# emitter is enough. dump_toml(cfg) -> loads_config() gives back an equal Config.

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_TOML_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t",
                 "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def _toml_str(s: str) -> str:
    out = []
    for ch in s:
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\u%04X" % ord(ch))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_key(k: str) -> str:
    return k if _BARE_KEY.fullmatch(k) else _toml_str(k)


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "nan"
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        return repr(v)
    if isinstance(v, str):
        return _toml_str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise ValueError(f"cannot write {type(v).__name__} value {v!r} as TOML")


def dump_toml(cfg: Union[Config, dict], header: Optional[str] = None) -> str:
    """Serialise a Config (or a to_dict()-shaped dict) as TOML, every key explicit."""
    d = cfg.to_dict() if isinstance(cfg, Config) else cfg
    lines = []
    for h in (header or "water-bacnet configuration (see config.example.toml for notes)").splitlines():
        lines.append(f"# {h}".rstrip())
    for section in _SECTIONS:
        values = d.get(section) or {}
        lines.append("")
        lines.append(f"[{section}]")
        tables = []
        for key, value in values.items():
            if isinstance(value, dict):
                tables.append((key, value))
            elif value is not None:
                lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        for key, table in tables:
            if not table:
                continue
            lines.append("")
            lines.append(f"[{section}.{_toml_key(key)}]")
            for k, v in table.items():
                lines.append(f"{_toml_key(str(k))} = {_toml_value(str(v))}")
    return "\n".join(lines) + "\n"


_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_FREQ = re.compile(r"\d+(\.\d+)?[kMG]?")
_RATE = re.compile(r"\d+(\.\d+)?[kM]?")
# Same patterns as water_bacnet.helper uses for [recovery].
_VIDPID = re.compile(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}")
_HUB_LOC = re.compile(r"[0-9]+(-[0-9]+(\.[0-9]+)*)?")
_HUB_PORT = re.compile(r"[0-9]+(,[0-9]+)*")


def problems(cfg: Config) -> list:
    """Stricter checks than load_config, applied before the portal saves a config.

    load_config stays lenient so a hand-edited file keeps the service running; this
    catches values that would parse but cannot work. Returns human-readable strings.
    """
    out = []

    def rng(where, value, lo, hi):
        if not (lo <= value <= hi) or (isinstance(value, float) and not math.isfinite(value)):
            out.append(f"{where} must be between {lo} and {hi}, got {value}")

    def text(where, value, allow_empty=False):
        if not value and not allow_empty:
            out.append(f"{where} must not be empty")
        elif _CTRL.search(value):
            out.append(f"{where} must not contain control characters")

    def abspath(where, value):
        text(where, value)
        if value and not value.startswith("/"):
            out.append(f"{where} must be an absolute path (start with /)")

    m, s, st, b, p = cfg.meter, cfg.source, cfg.storage, cfg.bacnet, cfg.portal
    r, h = cfg.recovery, cfg.health
    if m.id == 0:
        out.append("[meter] id must be set: the on-air ORION id, i.e. the low 24 bits of the "
                   "serial stamped on the endpoint (serial & 0xFFFFFF)")
    else:
        rng("[meter] id", m.id, 1, 0xFFFFFFFF)
    rng("[meter] stale_after_s", m.stale_after_s, 60, 86400)
    if s.mode not in ("rtl_433", "state_file"):
        out.append('[source] mode must be "rtl_433" or "state_file"')
    abspath("[source] state_file", s.state_file)
    text("[source] rtl433_path", s.rtl433_path)
    if s.device and not re.fullmatch(r"\S+", s.device):
        out.append("[source] device must not contain spaces")
    if not _FREQ.fullmatch(s.frequency):
        out.append(f'[source] frequency must look like "916.45M", got {s.frequency!r}')
    if not _RATE.fullmatch(s.sample_rate):
        out.append(f'[source] sample_rate must look like "1600k", got {s.sample_rate!r}')
    rng("[source] protocol", s.protocol, 1, 9999)
    rng("[source] restart_if_silent_s", s.restart_if_silent_s, 300, 7 * 86400)
    for a in s.extra_args:
        text("[source] extra_args", a)
    abspath("[storage] state_dir", st.state_dir)
    rng("[storage] raw_retention_days", st.raw_retention_days, 1, 3650)
    rng("[storage] save_interval_s", st.save_interval_s, 10, 86400)
    if b.bind != "auto":
        if "/" not in b.bind:
            out.append('[bacnet] bind must be "auto" or address/prefix, e.g. "192.0.2.10/24"')
        else:
            try:
                parse_bind(b.bind)
            except ValueError as exc:
                out.append(str(exc))
    rng("[bacnet] port", b.port, 1, 65535)
    rng("[bacnet] device_instance", b.device_instance, 0, 4194302)
    rng("[bacnet] vendor_id", b.vendor_id, 0, 65535)
    rng("[bacnet] refresh_s", b.refresh_s, 1, 300)
    for where, value in (("[bacnet] device_name", b.device_name),
                         ("[bacnet] model_name", b.model_name)):
        text(where, value)
        if len(value) > 64:
            out.append(f"{where} must be at most 64 characters")
    text("[bacnet] description", b.description, allow_empty=True)
    text("[bacnet] location", b.location, allow_empty=True)
    for k, v in b.object_names.items():
        if not re.fullmatch(r"(analogInput|binaryInput):\d+", k):
            out.append(f'[bacnet.object_names] key must look like "analogInput:1", got {k!r}')
        text(f"[bacnet.object_names] {k}", v)
    try:
        ipaddress.ip_address(p.listen)
    except ValueError:
        out.append(f"[portal] listen must be an IP address (0.0.0.0 = all), got {p.listen!r}")
    rng("[portal] port", p.port, 1, 65535)
    # [recovery]: the same limits the root helper enforces (water_bacnet.helper), so a
    # value the portal accepts is never silently replaced by a default there.
    text("[recovery] uhubctl_path", r.uhubctl_path)
    if r.uhubctl_path.startswith("-"):
        out.append("[recovery] uhubctl_path must not start with -")
    if not _HUB_LOC.fullmatch(r.hub_location):
        out.append(f'[recovery] hub_location must look like "1-1" or "1-1.2", got {r.hub_location!r}')
    if r.hub_port and not _HUB_PORT.fullmatch(r.hub_port):
        out.append(f'[recovery] hub_port must be empty (all ports) or like "2" / "1,3", '
                   f'got {r.hub_port!r}')
    rng("[recovery] cycle_delay_s", r.cycle_delay_s, 1, 60)
    if not _VIDPID.fullmatch(r.usb_vid_pid):
        out.append(f'[recovery] usb_vid_pid must look like "0bda:2838", got {r.usb_vid_pid!r}')
    rng("[recovery] max_power_cycles_per_day", r.max_power_cycles_per_day, 0, 48)
    rng("[recovery] max_reboots_per_week", r.max_reboots_per_week, 0, 7)
    rng("[recovery] step_timeout_s", r.step_timeout_s, 10, 600)
    # [health]
    abspath("[health] thermal_path", h.thermal_path)
    text("[health] vcgencmd_path", h.vcgencmd_path)
    if h.enclosure_sensor not in ENCLOSURE_SENSORS:
        out.append(f"[health] enclosure_sensor must be one of {', '.join(ENCLOSURE_SENSORS)}, "
                   f"got {h.enclosure_sensor!r}")
    rng("[health] i2c_bus", h.i2c_bus, 0, 255)
    if h.i2c_address not in (0, 0x44, 0x45, 0x76, 0x77):
        out.append("[health] i2c_address must be 0 (probe), 0x44/0x45 (SHT31) or 0x76/0x77 "
                   f"(BME280), got {h.i2c_address}")
    rng("[health] poll_s", h.poll_s, 5, 3600)
    return out


def validate(text: str, path: Optional[str] = None) -> Config:
    """Parse + strictly check config text. Raises ValueError listing every problem."""
    cfg = loads_config(text, path)
    found = problems(cfg)
    if found:
        raise ValueError("; ".join(found))
    return cfg
