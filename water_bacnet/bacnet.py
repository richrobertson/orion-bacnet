"""BACnet/IP server: exposes the latest meter Reading as read-only BACnet objects.

Built on bacpypes3 0.0.110 (``bacpypes3.ipv4.app.NormalApplication``).

Objects (see docs/DESIGN.md; numbers are fixed -- new objects are only ever appended):
    analogInput 1..9   meter values, statusFlags.fault + reliability on stale/unreadable
    analogInput 10, 11 recovery counters (USB resets, USB power cycles) from the Reading
    analogInput 12..15 box health: CPU temp, throttle flags (raw), enclosure temp / RH
                       (water_bacnet.health); a missing sensor -> reliability noSensor
    binaryInput 1      "Feed Stale" -- active when the feed is stale or unreadable
    binaryInput 2      "Throttled/Undervoltage" -- any of get_throttled bits 0-3 set now

Liveness: READY=1 to systemd (Type=notify) once the sockets are bound, WATCHDOG=1 from
the refresh loop only while the source's own loop is alive, and the bind address is
re-checked every 60 s (gone, or bind="auto" now resolving elsewhere, for > 2 min ->
raise, so systemd restarts us on the new address).

Broadcast reception (verified in bacpypes3/ipv4/__init__.py, IPv4DatagramServer):
binding to "a.b.c.d/nn:port" opens one UDP socket on a.b.c.d:port (unicast) and,
on non-Windows hosts, a SECOND socket on the subnet broadcast address
(e.g. 192.0.2.255:port) with SO_REUSEPORT. On Linux a socket bound to a unicast
address never sees broadcasts, so that second socket is what receives a
subnet-directed Who-Is. Neither socket receives the *limited* broadcast
255.255.255.255, which some clients use (it is the default "broadcast address"
of BACnet4J-based clients such as Mango Automation), so on Linux this module adds a
third listener on 255.255.255.255:port that feeds the same stack. I-Am is sent back unicast to the
requester (WhoIsIAmServices.do_WhoIsRequest -> i_am(address=pduSource)).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import platform
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

from bacpypes3.basetypes import (
    BinaryPV,
    EngineeringUnits,
    EventState,
    Polarity,
    Reliability,
    StatusFlags,
)
from bacpypes3.errors import ExecutionError
from bacpypes3.ipv4 import IPv4DatagramProtocol
from bacpypes3.ipv4.app import NormalApplication
from bacpypes3.local.analog import AnalogInputObject as _LocalAnalogInput
from bacpypes3.local.binary import BinaryInputObject as _LocalBinaryInput
from bacpypes3.local.device import DeviceObject
from bacpypes3.pdu import IPv4Address, LocalBroadcast

from . import __version__, sdnotify
from .health import HealthMonitor, HealthReading

log = logging.getLogger(__name__)

CUFT_TO_USGAL = 7.480519

# bacpypes3 0.0.110 has EngineeringUnits.cubicFeetPerHour (191), so flow is
# published per hour as-is; the /60 fallback only exists for older enums.
if hasattr(EngineeringUnits, "cubicFeetPerHour"):
    FLOW_UNITS = EngineeringUnits.cubicFeetPerHour
    FLOW_SCALE = 1.0
    FLOW_NOTE = "cu ft per hour"
else:  # pragma: no cover - not the case in the pinned version
    FLOW_UNITS = EngineeringUnits.cubicFeetPerMinute
    FLOW_SCALE = 1.0 / 60.0
    FLOW_NOTE = "cu ft per MINUTE (value = per-hour / 60; enum lacks cubicFeetPerHour)"

DB_UNITS = getattr(EngineeringUnits, "decibels", EngineeringUnits.noUnits)
RH_UNITS = getattr(EngineeringUnits, "percentRelativeHumidity", EngineeringUnits.percent)

# Liveness timings (seconds). Class attributes on MeterBacnetServer copy these so
# tests can shorten them.
SOURCE_ALIVE_S = 60.0  # WATCHDOG=1 only while source.heartbeat() is younger than this
BIND_CHECK_S = 60.0
BIND_GRACE_S = 120.0
MAX_TICK_S = 20.0  # refresh loop wakes at least this often (watchdog pings)


# --------------------------------------------------------------------------
# Read-only object classes
# --------------------------------------------------------------------------


class _ReadOnlyMixin:
    """Reject every WriteProperty from the network.

    Local updates use plain attribute assignment, which does not go through
    write_property(), so the server itself can still change values.
    """

    async def write_property(self, attr, value, index=None, priority=None):  # type: ignore[override]
        raise ExecutionError(errorClass="property", errorCode="writeAccessDenied")


class ReadOnlyAnalogInput(_ReadOnlyMixin, _LocalAnalogInput):
    pass


class ReadOnlyBinaryInput(_ReadOnlyMixin, _LocalBinaryInput):
    pass


class ReadOnlyDevice(_ReadOnlyMixin, DeviceObject):
    pass


# --------------------------------------------------------------------------
# Object table
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AISpec:
    instance: int
    name: str
    units: Any
    description: str
    getter: Callable[[Any], Optional[float]]
    # "meter": from the Reading, faulted (communicationFailure) while the feed is stale.
    # "counter": from the Reading, valid even while the feed is stale (that is exactly
    #            when the recovery counters matter).
    # "health": from the HealthReading; None -> reliability noSensor.
    kind: str = "meter"


def _scaled(attr: str, scale: float) -> Callable[[Any], Optional[float]]:
    def get(r: Any) -> Optional[float]:
        v = getattr(r, attr, None)
        return None if v is None else float(v) * scale

    return get


AI_TABLE: Tuple[AISpec, ...] = (
    AISpec(1, "Meter Register", EngineeringUnits.cubicFeet,
           "Meter register, CUBIC FEET (1:1 with the dial, 1 cu ft resolution)",
           _scaled("volume_cuft", 1.0)),
    AISpec(2, "Meter Register (gallons)", EngineeringUnits.usGallons,
           "Meter register converted to US gallons (cu ft x 7.480519)",
           _scaled("volume_cuft", CUFT_TO_USGAL)),
    AISpec(3, "Flow Rate", FLOW_UNITS,
           f"Average flow over ~6 h of register history, {FLOW_NOTE}",
           _scaled("flow_per_hour", FLOW_SCALE)),
    AISpec(4, "Flow Rate (recent)", FLOW_UNITS,
           f"Average flow over the last ~1 h, {FLOW_NOTE}",
           _scaled("flow_recent_per_hour", FLOW_SCALE)),
    AISpec(5, "Seconds Since Packet", EngineeringUnits.seconds,
           "Live age of the last accepted meter packet (keeps growing if the feed freezes)",
           _scaled("seconds_since_packet", 1.0)),
    AISpec(6, "Signal SNR", DB_UNITS, "SNR of the last packet as reported by rtl_433 (dB)",
           _scaled("snr", 1.0)),
    AISpec(7, "Signal RSSI", DB_UNITS, "RSSI of the last packet as reported by rtl_433 (dB, relative)",
           _scaled("rssi", 1.0)),
    AISpec(8, "Packets Per Hour", EngineeringUnits.perHour,
           "Accepted packets in the last hour (~22/h is the healthy baseline)",
           _scaled("packets_per_hour", 1.0)),
    AISpec(9, "Collector rtl_433 Restarts", EngineeringUnits.noUnits,
           "Number of times the collector has restarted rtl_433",
           _scaled("rtl433_restarts", 1.0)),
    AISpec(10, "USB Resets", EngineeringUnits.noUnits,
           "Recovery ladder: USBDEVFS_RESET attempts on the RTL-SDR dongle",
           _scaled("usb_resets", 1.0), "counter"),
    AISpec(11, "USB Power Cycles", EngineeringUnits.noUnits,
           "Recovery ladder: USB port power cycles (uhubctl) of the RTL-SDR dongle",
           _scaled("power_cycles", 1.0), "counter"),
    AISpec(12, "CPU Temperature", EngineeringUnits.degreesCelsius,
           "Raspberry Pi SoC temperature (throttling starts at ~80 C)",
           _scaled("cpu_temp_c", 1.0), "health"),
    AISpec(13, "Throttled Flags (raw)", EngineeringUnits.noUnits,
           "vcgencmd get_throttled as a number: bits 0-3 now (under-voltage, capped, "
           "throttled, soft temp limit), bits 16-19 since boot",
           _scaled("throttled", 1.0), "health"),
    AISpec(14, "Enclosure Temperature", EngineeringUnits.degreesCelsius,
           "Air temperature inside the enclosure (SHT31/BME280 on I2C)",
           _scaled("enclosure_temp_c", 1.0), "health"),
    AISpec(15, "Enclosure Humidity", RH_UNITS,
           "Relative humidity inside the enclosure; a rising trend means the seal is failing",
           _scaled("enclosure_rh", 1.0), "health"),
)

BI_NAME = "Feed Stale"
BI_DESCRIPTION = "ACTIVE when no fresh meter data (packet age or feed older than stale limit, or feed unreadable). Alarm on this."
BI2_NAME = "Throttled/Undervoltage"
BI2_DESCRIPTION = ("ACTIVE while the Pi reports under-voltage, frequency capping, throttling or "
                   "the soft temperature limit (get_throttled bits 0-3). Alarm on this.")


def _status_flags(fault: bool) -> StatusFlags:
    # bit order: inAlarm, fault, overridden, outOfService
    return StatusFlags([0, 1 if fault else 0, 0, 0])


# --------------------------------------------------------------------------
# Bind address resolution
# --------------------------------------------------------------------------


def _linux_default_route_addr() -> Tuple[str, int]:
    out = subprocess.run(
        ["ip", "-j", "route", "get", "1.1.1.1"],
        check=True, capture_output=True, text=True, timeout=5,
    ).stdout
    route = json.loads(out)[0]
    dev = route["dev"]
    src = route.get("prefsrc")
    out = subprocess.run(
        ["ip", "-j", "-4", "addr", "show", "dev", dev],
        check=True, capture_output=True, text=True, timeout=5,
    ).stdout
    infos = [ai for link in json.loads(out) for ai in link.get("addr_info", [])
             if ai.get("family") == "inet"]
    if not infos:
        raise RuntimeError(f"no IPv4 address on default-route interface {dev}")
    for ai in infos:
        if src is None or ai["local"] == src:
            return ai["local"], int(ai["prefixlen"])
    return infos[0]["local"], int(infos[0]["prefixlen"])


def _udp_connect_addr() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 9))  # no packet is sent for UDP connect
        return s.getsockname()[0]
    finally:
        s.close()


def resolve_bind(bind: str) -> Tuple[str, int]:
    """Return (ip, prefixlen) for the configured bind ("auto" or "a.b.c.d/nn")."""
    bind = (bind or "auto").strip()
    if bind.lower() != "auto":
        iface = ipaddress.IPv4Interface(bind if "/" in bind else bind + "/24")
        if "/" not in bind:
            log.warning("bind %r has no prefix; assuming /24", bind)
        return str(iface.ip), iface.network.prefixlen
    if platform.system() == "Linux":
        try:
            return _linux_default_route_addr()
        except Exception as exc:  # fall through to the portable trick
            log.warning("bind=auto: 'ip -j' lookup failed (%s); falling back", exc)
    ip = _udp_connect_addr()
    log.warning("bind=auto: using %s and ASSUMING /24 (no 'ip' command); set bind explicitly", ip)
    return ip, 24


async def _wait_for_address(ip: str, timeout: float = 60.0) -> None:
    """Wait until ``ip`` is assigned locally (bridges/VLAN interfaces can come up late)."""
    deadline = time.monotonic() + timeout
    warned = False
    while True:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((ip, 0))
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"bind address {ip} not present after {timeout:.0f}s: {exc}")
            if not warned:
                log.warning("bind address %s not present yet (%s); waiting up to %.0fs", ip, exc, timeout)
                warned = True
        finally:
            s.close()
        await asyncio.sleep(1.0)


def address_is_local(ip: str) -> bool:
    """True if `ip` is assigned to this host (a socket can bind to it)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


async def _check_endpoints(app: NormalApplication, address: IPv4Address, timeout: float = 15.0) -> None:
    """Make sure bacpypes3 actually opened its sockets.

    bacpypes3 0.0.110 retries a failed bind forever, silently, and
    IPv4DatagramServer.indication() awaits ALL endpoint tasks (unicast AND
    broadcast) before sending anything. So a broadcast socket that can never
    bind would make the device mute -- no I-Am, no ReadProperty replies -- with
    nothing in the log. Here: unicast must come up or we raise (systemd
    restarts us); a broadcast socket that won't bind is logged as an ERROR and
    no longer waited on, so unicast service still works.
    """
    server = app.normal.server
    tasks = list(getattr(server, "_transport_tasks", []) or [])
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    local_task = tasks[0]
    if local_task in pending:
        for t in tasks:
            t.cancel()
        raise RuntimeError(f"could not bind BACnet socket {address.addrTuple} within {timeout:.0f}s")
    if pending:
        # leave the task running: bacpypes3 keeps retrying the bind, so the
        # broadcast socket still comes up if the address appears later
        log.error(
            "could not bind broadcast socket %s:%d within %.0fs -- broadcast Who-Is "
            "will NOT be answered until it binds (directed Who-Is/ReadProperty work)",
            address.addrBroadcastTuple[0], address.addrBroadcastTuple[1], timeout,
        )
    server._transport_tasks = []


class _LimitedBroadcastProtocol(IPv4DatagramProtocol):
    """Datagrams on 255.255.255.255 are handed up as local broadcasts."""

    def connection_made(self, transport):  # type: ignore[override]
        self.destination = LocalBroadcast()


async def _add_limited_broadcast_listener(app: NormalApplication, port: int):
    if platform.system() != "Linux":
        return None
    server = app.normal.server
    loop = asyncio.get_running_loop()

    def factory():
        p = _LimitedBroadcastProtocol()
        p.server = server
        return p

    try:
        transport, _ = await loop.create_datagram_endpoint(
            factory, local_addr=("255.255.255.255", port),
            allow_broadcast=True, reuse_port=True,
        )
        log.info("listening for limited broadcasts on 255.255.255.255:%d", port)
        return transport
    except OSError as exc:
        log.warning("could not listen on 255.255.255.255:%d (%s); subnet broadcast still works", port, exc)
        return None


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------


class MeterBacnetServer:
    """Owns the bacpypes3 application and the meter objects."""

    def __init__(self, cfg: Any, source: Any, health: Optional[HealthMonitor] = None,
                 on_ready: Optional[Callable[[dict], None]] = None) -> None:
        self.cfg = cfg
        self.source = source
        b = cfg.bacnet
        self.port = int(b.port)
        self.refresh_s = float(getattr(b, "refresh_s", 15.0))
        names = dict(getattr(b, "object_names", {}) or {})
        meter_id = getattr(getattr(cfg, "meter", None), "id", 0)

        self.device = ReadOnlyDevice(
            objectIdentifier=("device", int(b.device_instance)),
            objectName=b.device_name,
            vendorIdentifier=int(b.vendor_id),
            vendorName="orion-bacnet (unregistered)",
            modelName=getattr(b, "model_name", "Badger ORION via rtl_433"),
            description=getattr(
                b, "description",
                f"Water meter (Badger ORION endpoint, ORION id {meter_id})"),
            firmwareRevision=__version__,
            applicationSoftwareVersion=__version__,
            location=getattr(b, "location", ""),
        )

        self.ai: List[Tuple[AISpec, ReadOnlyAnalogInput]] = []
        for spec in AI_TABLE:
            key = f"analogInput:{spec.instance}"
            obj = ReadOnlyAnalogInput(
                objectIdentifier=("analogInput", spec.instance),
                objectName=names.get(key, spec.name),
                description=spec.description,
                presentValue=0.0,
                units=spec.units,
                statusFlags=_status_flags(True),
                eventState=EventState.normal,
                reliability=Reliability.communicationFailure,
                outOfService=False,
                covIncrement=0.0,
            )
            self.ai.append((spec, obj))

        self.bi = ReadOnlyBinaryInput(
            objectIdentifier=("binaryInput", 1),
            objectName=names.get("binaryInput:1", BI_NAME),
            description=BI_DESCRIPTION,
            presentValue=BinaryPV.active,
            statusFlags=_status_flags(False),
            eventState=EventState.normal,
            reliability=Reliability.noFaultDetected,
            outOfService=False,
            polarity=Polarity.normal,
            activeText="STALE",
            inactiveText="OK",
        )
        self.bi2 = ReadOnlyBinaryInput(
            objectIdentifier=("binaryInput", 2),
            objectName=names.get("binaryInput:2", BI2_NAME),
            description=BI2_DESCRIPTION,
            presentValue=BinaryPV.inactive,
            statusFlags=_status_flags(True),
            eventState=EventState.normal,
            reliability=Reliability.noSensor,
            outOfService=False,
            polarity=Polarity.normal,
            activeText="THROTTLED",
            inactiveText="OK",
        )

        self.health = health
        self.on_ready = on_ready
        self.app: Optional[NormalApplication] = None
        self._extra_transport = None
        self._last_state: Optional[str] = None
        self.bound_ip: Optional[str] = None
        self.ready = False
        self.source_alive_s = SOURCE_ALIVE_S
        self.bind_check_s = BIND_CHECK_S
        self.bind_grace_s = BIND_GRACE_S
        self._bind_bad_since: Optional[float] = None
        self._watchdog_withheld = False
        wd = sdnotify.watchdog_interval()
        # Ping at least 3x per WatchdogSec; wake at least every MAX_TICK_S.
        self.tick_s = min(self.refresh_s, MAX_TICK_S, wd / 3 if wd else MAX_TICK_S)

    async def start(self) -> None:
        ip, prefix = resolve_bind(self.cfg.bacnet.bind)
        self.bound_ip = ip
        await _wait_for_address(ip)
        address = IPv4Address(f"{ip}/{prefix}:{self.port}")
        self.app = NormalApplication(self.device, address)
        for _, obj in self.ai:
            self.app.add_object(obj)
        self.app.add_object(self.bi)
        self.app.add_object(self.bi2)
        await _check_endpoints(self.app, address)
        self._extra_transport = await _add_limited_broadcast_listener(self.app, self.port)
        log.info(
            "BACnet device %s '%s' on %s (broadcast %s:%d), %d objects",
            self.cfg.bacnet.device_instance, self.cfg.bacnet.device_name, address,
            address.addrBroadcastTuple[0], self.port, len(self.ai) + 2,
        )
        self.address = str(address)

    def refresh(self, now: Optional[float] = None) -> None:
        """Push source.latest(now) into the objects."""
        now = time.time() if now is None else now
        try:
            r = self.source.latest(now)
            stale = bool(getattr(r, "stale", True))
            error = getattr(r, "error", None)
        except Exception as exc:  # a broken source must look broken, never healthy
            log.exception("source.latest() failed")
            r, stale, error = None, True, f"source error: {exc}"

        feed_bad = stale or r is None
        h = self._health_reading()
        for spec, obj in self.ai:
            src = h if spec.kind == "health" else r
            value = None
            if src is not None:
                try:
                    value = spec.getter(src)
                except (TypeError, ValueError):
                    value = None
            if value is not None and value != value:  # NaN never reaches BACnet
                value = None
            if value is not None:
                obj.presentValue = value  # else keep the last good value
            if spec.kind == "health":
                fault, why = value is None, Reliability.noSensor
            elif spec.kind == "counter":
                fault, why = value is None, Reliability.communicationFailure
            else:
                fault, why = feed_bad or value is None, Reliability.communicationFailure
            obj.statusFlags = _status_flags(fault)
            obj.reliability = why if fault else Reliability.noFaultDetected

        self.bi.presentValue = BinaryPV.active if feed_bad else BinaryPV.inactive

        thr = h.throttled_now if h is not None else None
        if thr is None:
            self.bi2.statusFlags = _status_flags(True)
            self.bi2.reliability = Reliability.noSensor
        else:
            self.bi2.presentValue = BinaryPV.active if thr else BinaryPV.inactive
            self.bi2.statusFlags = _status_flags(False)
            self.bi2.reliability = Reliability.noFaultDetected

        state = "stale" if feed_bad else "fresh"
        if state != self._last_state:
            if feed_bad:
                log.warning("BACnet: feed %s -> STALE (fault flags set; BI 1 active)%s",
                            self._last_state or "startup", f": {error}" if error else "")
            else:
                log.info("BACnet: feed %s -> fresh (fault flags cleared)", self._last_state or "startup")
            self._last_state = state

    def _health_reading(self) -> Optional[HealthReading]:
        if self.health is None:
            return None
        try:
            return self.health.latest()
        except Exception:
            log.exception("health.latest() failed")
            return None

    # ---- liveness ------------------------------------------------------------------

    def _mark_ready(self) -> None:
        self.ready = True
        sdnotify.ready(f"serving BACnet device {self.cfg.bacnet.device_instance} on "
                       f"{getattr(self, 'address', '?')}")
        if self.on_ready is not None:
            try:
                self.on_ready({"address": getattr(self, "address", None)})
            except Exception:
                log.exception("on_ready hook failed")

    def source_alive(self, mono: Optional[float] = None) -> bool:
        """The source's own loop completed an iteration within source_alive_s."""
        hb = getattr(self.source, "heartbeat", None)
        if not callable(hb):  # older source without the liveness hook
            return True
        mono = time.monotonic() if mono is None else mono
        try:
            return mono - float(hb()) < self.source_alive_s
        except Exception:
            log.exception("source.heartbeat() failed")
            return False

    def watchdog_ping(self) -> bool:
        """WATCHDOG=1, but only while the source loop is alive. -> pinged?"""
        if self.source_alive():
            if self._watchdog_withheld:
                log.warning("source loop alive again; resuming systemd watchdog pings")
                self._watchdog_withheld = False
            sdnotify.watchdog()
            return True
        if not self._watchdog_withheld:
            log.error("source loop has not completed an iteration for %.0f s; withholding "
                      "the systemd watchdog ping (systemd will restart us after WatchdogSec)",
                      self.source_alive_s)
            self._watchdog_withheld = True
        return False

    def bind_ok(self) -> Tuple[bool, str]:
        """Blocking (may run `ip`): is the address we serve on still the right one?"""
        ip = self.bound_ip
        if ip is None:
            return True, ""
        if not address_is_local(ip):
            return False, f"bound address {ip} is no longer assigned to this host"
        if (self.cfg.bacnet.bind or "auto").strip().lower() == "auto":
            try:
                now_ip, _ = resolve_bind("auto")
            except Exception as exc:  # no default route right now; we still have ip
                log.debug("bind=auto re-resolve failed: %s", exc)
                return True, ""
            if now_ip != ip:
                return False, f'bind = "auto" now resolves to {now_ip}, but we serve on {ip}'
        return True, ""

    async def check_bind(self, mono: Optional[float] = None) -> None:
        """Raise RuntimeError once the bind has been wrong for longer than bind_grace_s."""
        ok, why = await asyncio.to_thread(self.bind_ok)
        mono = time.monotonic() if mono is None else mono
        if ok:
            if self._bind_bad_since is not None:
                log.info("BACnet bind address %s is fine again", self.bound_ip)
            self._bind_bad_since = None
            return
        if self._bind_bad_since is None:
            self._bind_bad_since = mono
            log.warning("%s; restarting if this lasts %.0f s", why, self.bind_grace_s)
        elif mono - self._bind_bad_since > self.bind_grace_s:
            raise RuntimeError(f"{why} for {mono - self._bind_bad_since:.0f} s; exiting so "
                               "systemd restarts the service on the current address")

    async def run(self) -> None:
        await self.start()
        self._mark_ready()
        next_refresh = 0.0
        next_bind = time.monotonic() + self.bind_check_s
        while True:
            mono = time.monotonic()
            if mono >= next_refresh:
                self.refresh()
                next_refresh = mono + self.refresh_s
            if mono >= next_bind:
                await self.check_bind()
                next_bind = time.monotonic() + self.bind_check_s
            self.watchdog_ping()
            await asyncio.sleep(max(0.01, min(self.tick_s, next_refresh - time.monotonic())))

    def close(self) -> None:
        if self._extra_transport is not None:
            self._extra_transport.close()
        if self.app is not None:
            self.app.close()


async def serve(cfg: Any, source: Any, health: Optional[HealthMonitor] = None,
                on_ready: Optional[Callable[[dict], None]] = None) -> None:
    """Run the BACnet server forever (until cancelled).

    health: a HealthMonitor to read AI12-15/BI2 from; None = create one from
    cfg.health and poll it here. on_ready(info) is called once the device is actually
    serving (sockets bound), right after READY=1 goes to systemd.
    """
    health_task = None
    if health is None:
        health = HealthMonitor(getattr(cfg, "health", None))
        health_task = asyncio.create_task(health.run(), name="health")
    server = MeterBacnetServer(cfg, source, health=health, on_ready=on_ready)
    try:
        await server.run()
    finally:
        if health_task is not None:
            health_task.cancel()
            await asyncio.gather(health_task, return_exceptions=True)
        server.close()
