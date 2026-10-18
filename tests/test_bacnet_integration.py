"""End-to-end: water_bacnet.bacnet.serve() on loopback, queried by a bacpypes3 client.

Binding notes (verified against bacpypes3 0.0.110 ipv4/__init__.py):
- The server binds "127.0.0.1/32". With a /8, bacpypes3 also tries to bind the
  broadcast address 127.255.255.255; macOS refuses that (EADDRNOTAVAIL), bacpypes3
  retries forever, and IPv4DatagramServer.indication() awaits that task before EVERY
  send -- so nothing is ever transmitted. /32 makes broadcast == unicast, which
  skips the broadcast listener and leaves directed traffic working.
- For the same reason a *broadcast* Who-Is cannot be exercised on loopback; that
  test is skipped with the reason. Directed Who-Is / ReadProperty / RPM are tested.

One server + one client live on an event loop in a background thread for the whole
module; tests submit coroutines to it.
"""

import argparse
import asyncio
import socket
import sys
import threading
import time
from dataclasses import replace

import pytest

from conftest import GAL_PER_CUFT, write_config

bacpypes3 = pytest.importorskip("bacpypes3")
from bacpypes3.apdu import ErrorRejectAbortNack  # noqa: E402
from bacpypes3.app import Application  # noqa: E402
from bacpypes3.basetypes import BinaryPV, EngineeringUnits, Reliability  # noqa: E402
from bacpypes3.pdu import Address  # noqa: E402

from water_bacnet.health import HealthReading  # noqa: E402
from water_bacnet.meter import Reading  # noqa: E402

DEVICE = 2801
REFRESH = 0.1

FRESH = Reading(
    volume_cuft=41774.0, flow_per_hour=0.5, flow_recent_per_hour=1.25,
    seconds_since_packet=221.0, snr=16.706, rssi=-8.626, freq=915.993,
    packets_per_hour=21.65, rtl433_restarts=3, packets_total=12114,
    stale=False, error=None, updated=time.time(),
    usb_resets=2, power_cycles=1,
)

HEALTHY = HealthReading(cpu_temp_c=51.2, throttled=0x50000, enclosure_temp_c=18.5,
                        enclosure_rh=44.0, enclosure_sensor="sht31@0x44",
                        updated=time.monotonic())
NO_SENSORS = HealthReading(updated=time.monotonic())

EXPECTED_AI = {
    1: FRESH.volume_cuft,
    2: FRESH.volume_cuft * GAL_PER_CUFT,
    3: FRESH.flow_per_hour,
    4: FRESH.flow_recent_per_hour,
    5: FRESH.seconds_since_packet,
    6: FRESH.snr,
    7: FRESH.rssi,
    8: FRESH.packets_per_hour,
    9: float(FRESH.rtl433_restarts),
    10: 2.0,
    11: 1.0,
    12: 51.2,
    13: float(0x50000),
    14: 18.5,
    15: 44.0,
}


class StubHealth:
    def __init__(self, reading):
        self.reading = reading

    def latest(self):
        return self.reading


class StubSource:
    def __init__(self, reading):
        self.reading = reading
        self.calls = 0

    def latest(self, now=None):
        self.calls += 1
        return self.reading


def _free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Harness:
    def __init__(self, tmp):
        self.port = _free_udp_port()
        self.client_port = _free_udp_port()
        self.addr = f"127.0.0.1:{self.port}"
        self.cfg = write_config(tmp / "config.toml", f"""
[meter]
id = 7654321
stale_after_s = 900
[source]
mode = "state_file"
[bacnet]
bind = "127.0.0.1/32"
port = {self.port}
device_instance = {DEVICE}
device_name = "WaterMeter"
vendor_id = 999
refresh_s = 1
location = "Test bench"
""")
        # load_config requires refresh_s >= 1; the tests push values much faster.
        self.cfg = replace(self.cfg, bacnet=replace(self.cfg.bacnet, refresh_s=REFRESH))
        self.source = StubSource(FRESH)
        self.health = StubHealth(HEALTHY)
        self.ready_info = []
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.server_task = None
        self.client = None

    def call(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _start(self):
        from water_bacnet.bacnet import serve
        self.server_task = asyncio.ensure_future(
            serve(self.cfg, self.source, health=self.health, on_ready=self.ready_info.append))
        ns = argparse.Namespace(
            name="pytest-client", instance=4194000, network=0,
            address=f"127.0.0.1/32:{self.client_port}", vendoridentifier=999,
            foreign=None, ttl=30, bbmd=None,
        )
        self.client = Application.from_args(ns)
        # wait until the server has pulled from the source and is answering
        for _ in range(50):
            await asyncio.sleep(0.1)
            if self.server_task.done():
                self.server_task.result()
                raise AssertionError("serve() returned")
            if self.source.calls:
                break

    async def _stop(self):
        if self.server_task:
            self.server_task.cancel()
            try:
                await self.server_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.client:
            self.client.close()

    def read(self, obj, prop):
        return self.call(asyncio.wait_for(self.client.read_property(self.addr, obj, prop), 5))

    def set_reading(self, r):
        if self.source.reading is r:
            return
        self.source.reading = r
        time.sleep(REFRESH * 5)  # let serve() push it into the objects

    def set_health(self, hr):
        if self.health.reading is hr:
            return
        self.health.reading = hr
        time.sleep(REFRESH * 5)


@pytest.fixture(scope="module")
def h(tmp_path_factory):
    h = Harness(tmp_path_factory.mktemp("bacnet"))
    try:
        h.call(h._start(), timeout=15)
    except Exception:
        h.call(h._stop())
        h.loop.call_soon_threadsafe(h.loop.stop)
        raise
    yield h
    h.call(h._stop())
    h.loop.call_soon_threadsafe(h.loop.stop)
    h.thread.join(5)


@pytest.fixture(autouse=True)
def fresh_reading(request):
    yield
    if "h" in request.fixturenames:
        request.getfixturevalue("h").set_reading(FRESH)
        request.getfixturevalue("h").set_health(HEALTHY)


def test_directed_who_is_gets_i_am(h):
    async def go():
        return await asyncio.wait_for(
            h.client.who_is(DEVICE, DEVICE, Address(h.addr), timeout=3), 5)
    i_ams = h.call(go())
    assert i_ams, "no I-Am in reply to directed Who-Is"
    ident = i_ams[0].iAmDeviceIdentifier
    assert ident[0] == "device" or str(ident[0]) == "device"
    assert ident[1] == DEVICE
    assert int(i_ams[0].vendorID) == 999


def test_who_is_out_of_range_is_ignored(h):
    async def go():
        return await asyncio.wait_for(
            h.client.who_is(1, 100, Address(h.addr), timeout=1), 3)
    assert h.call(go()) == []


@pytest.mark.skipif(sys.platform == "darwin", reason=(
    "macOS cannot bind 127.255.255.255, and bacpypes3 0.0.110 blocks all sends while "
    "it retries that bind, so broadcast Who-Is cannot be tested on loopback here; "
    "test it on the Pi/LAN (e.g. bacpypes3 'whois' from another host)."))
def test_broadcast_who_is():  # pragma: no cover - not runnable on loopback
    pytest.skip("needs a real broadcast-capable interface")


def test_device_properties(h):
    dev = f"device,{DEVICE}"
    assert str(h.read(dev, "object-name")) == "WaterMeter"
    assert int(h.read(dev, "vendor-identifier")) == 999
    assert str(h.read(dev, "model-name")) == "Badger ORION via rtl_433"
    assert str(h.read(dev, "description")) == "Water meter (Badger ORION endpoint, read with rtl_433)"
    assert str(h.read(dev, "location")) == "Test bench"


@pytest.mark.parametrize("inst", range(1, 16))
def test_ai_present_value(h, inst):
    v = h.read(f"analog-input,{inst}", "present-value")
    assert float(v) == pytest.approx(EXPECTED_AI[inst], rel=1e-6, abs=1e-4)


def test_ai2_is_gallons_not_cubic_feet(h):
    v1 = float(h.read("analog-input,1", "present-value"))
    v2 = float(h.read("analog-input,2", "present-value"))
    assert v1 == pytest.approx(41774.0)
    assert v2 == pytest.approx(41774.0 * 7.480519, rel=1e-6)  # 312,491.2 gal


def test_units(h):
    assert h.read("analog-input,1", "units") == EngineeringUnits.cubicFeet
    assert h.read("analog-input,2", "units") == EngineeringUnits.usGallons
    assert h.read("analog-input,3", "units") in (EngineeringUnits.cubicFeetPerHour,
                                                 EngineeringUnits.cubicFeetPerMinute)
    assert h.read("analog-input,4", "units") == h.read("analog-input,3", "units")
    assert h.read("analog-input,5", "units") == EngineeringUnits.seconds
    assert h.read("analog-input,8", "units") == EngineeringUnits.perHour


def test_object_names(h):
    assert str(h.read("analog-input,1", "object-name")) == "Meter Register"
    assert str(h.read("analog-input,2", "object-name")) == "Meter Register (gallons)"
    assert str(h.read("binary-input,1", "object-name")) == "Feed Stale"


def test_fresh_status(h):
    for inst in range(1, 10):
        flags = list(h.read(f"analog-input,{inst}", "status-flags"))
        assert flags[1] == 0, (inst, flags)  # [inAlarm, fault, overridden, outOfService]
    assert h.read("binary-input,1", "present-value") == BinaryPV.inactive


def test_read_property_multiple(h):
    async def go():
        return await asyncio.wait_for(h.client.read_property_multiple(
            Address(h.addr),
            ["analog-input,1", ["present-value", "units", "status-flags"],
             "analog-input,2", ["present-value"],
             "binary-input,1", ["present-value"]]), 5)
    res = h.call(go())
    got = {(str(o[0]), o[1], str(p)): v for (o, p, _i, v) in res}
    assert got[("analog-input", 1, "present-value")] == pytest.approx(41774.0)
    assert got[("analog-input", 1, "units")] == EngineeringUnits.cubicFeet
    assert list(got[("analog-input", 1, "status-flags")])[1] == 0
    assert got[("analog-input", 2, "present-value")] == pytest.approx(41774.0 * GAL_PER_CUFT, rel=1e-6)
    assert got[("binary-input", 1, "present-value")] == BinaryPV.inactive


def test_write_is_rejected(h):
    async def go():
        try:
            r = await asyncio.wait_for(
                h.client.write_property(h.addr, "analog-input,1", "present-value", 1.0), 5)
            return r
        except ErrorRejectAbortNack as exc:  # a BaseException in bacpypes3
            return exc
    res = h.call(go())
    assert isinstance(res, ErrorRejectAbortNack), f"write accepted: {res!r}"
    assert "write-access-denied" in str(res)
    assert float(h.read("analog-input,1", "present-value")) == pytest.approx(41774.0)


def test_stale_sets_fault_and_bi_active_then_recovers(h):
    stale = replace(FRESH, stale=True, error="state file not updated for 1200s",
                    seconds_since_packet=1421.0)
    h.set_reading(stale)
    for inst in range(1, 10):
        flags = list(h.read(f"analog-input,{inst}", "status-flags"))
        assert flags[1] == 1, (inst, flags)
        assert h.read(f"analog-input,{inst}", "reliability") == Reliability.communicationFailure
    assert h.read("binary-input,1", "present-value") == BinaryPV.active
    # last good value kept
    assert float(h.read("analog-input,1", "present-value")) == pytest.approx(41774.0)

    h.set_reading(FRESH)
    assert list(h.read("analog-input,1", "status-flags"))[1] == 0
    assert h.read("analog-input,1", "reliability") == Reliability.noFaultDetected
    assert h.read("binary-input,1", "present-value") == BinaryPV.inactive


def test_never_received_reading_is_faulted(h):
    h.set_reading(Reading.empty("no packet received yet") if hasattr(Reading, "empty") else
                  Reading(None, None, None, None, None, None, None, None, 0, 0, True,
                          "no packet received yet", 0.0))
    assert list(h.read("analog-input,1", "status-flags"))[1] == 1
    assert h.read("binary-input,1", "present-value") == BinaryPV.active


# ---------------------------------------------------------------------------------
# AI10-15 / BI2 (appended objects) and liveness

def test_on_ready_called_once_serving(h):
    assert h.ready_info and h.ready_info[0]["address"].startswith("127.0.0.1")


def test_original_numbering_unchanged(h):
    names = {i: str(h.read(f"analog-input,{i}", "object-name")) for i in range(1, 16)}
    assert names[1] == "Meter Register" and names[9] == "Collector rtl_433 Restarts"
    assert names[10] == "USB Resets" and names[11] == "USB Power Cycles"
    assert names[12] == "CPU Temperature" and names[13] == "Throttled Flags (raw)"
    assert names[14] == "Enclosure Temperature" and names[15] == "Enclosure Humidity"
    assert str(h.read("binary-input,1", "object-name")) == "Feed Stale"
    assert str(h.read("binary-input,2", "object-name")) == "Throttled/Undervoltage"


def test_new_units(h):
    assert h.read("analog-input,10", "units") == EngineeringUnits.noUnits
    assert h.read("analog-input,11", "units") == EngineeringUnits.noUnits
    assert h.read("analog-input,12", "units") == EngineeringUnits.degreesCelsius
    assert h.read("analog-input,13", "units") == EngineeringUnits.noUnits
    assert h.read("analog-input,14", "units") == EngineeringUnits.degreesCelsius
    assert h.read("analog-input,15", "units") == EngineeringUnits.percentRelativeHumidity


def test_health_present_is_not_faulted(h):
    for inst in range(10, 16):
        assert list(h.read(f"analog-input,{inst}", "status-flags"))[1] == 0, inst
        assert h.read(f"analog-input,{inst}", "reliability") == Reliability.noFaultDetected
    # 0x50000: throttling happened since boot, but not NOW -> BI2 inactive
    assert h.read("binary-input,2", "present-value") == BinaryPV.inactive
    assert list(h.read("binary-input,2", "status-flags"))[1] == 0


def test_bi2_active_on_current_undervoltage(h):
    h.set_health(replace(HEALTHY, throttled=0x50005))
    assert h.read("binary-input,2", "present-value") == BinaryPV.active
    assert float(h.read("analog-input,13", "present-value")) == float(0x50005)
    h.set_health(replace(HEALTHY, throttled=0x0))
    assert h.read("binary-input,2", "present-value") == BinaryPV.inactive


def test_absent_sensors_are_no_sensor_faults(h):
    h.set_health(NO_SENSORS)
    for inst in range(12, 16):
        assert list(h.read(f"analog-input,{inst}", "status-flags"))[1] == 1, inst
        assert h.read(f"analog-input,{inst}", "reliability") == Reliability.noSensor, inst
    # last known value is kept
    assert float(h.read("analog-input,12", "present-value")) == pytest.approx(51.2)
    assert list(h.read("binary-input,2", "status-flags"))[1] == 1
    assert h.read("binary-input,2", "reliability") == Reliability.noSensor
    # the meter feed is unaffected by missing health sensors
    assert list(h.read("analog-input,1", "status-flags"))[1] == 0
    assert h.read("binary-input,1", "present-value") == BinaryPV.inactive
    h.set_health(HEALTHY)
    assert h.read("analog-input,14", "reliability") == Reliability.noFaultDetected


def test_recovery_counters_stay_valid_while_feed_is_stale(h):
    h.set_reading(replace(FRESH, stale=True, error="no packet", usb_resets=3, power_cycles=2))
    assert list(h.read("analog-input,1", "status-flags"))[1] == 1
    assert float(h.read("analog-input,10", "present-value")) == 3.0
    assert float(h.read("analog-input,11", "present-value")) == 2.0
    for inst in (10, 11):
        assert list(h.read(f"analog-input,{inst}", "status-flags"))[1] == 0
    # health objects do not follow the feed either
    assert list(h.read("analog-input,12", "status-flags"))[1] == 0


def test_old_reading_without_recovery_fields_still_works(h):
    class OldReading:  # an object without usb_resets / power_cycles
        def __init__(self):
            for k in ("volume_cuft", "flow_per_hour", "flow_recent_per_hour",
                      "seconds_since_packet", "snr", "rssi", "freq", "packets_per_hour",
                      "rtl433_restarts", "packets_total", "stale", "error", "updated"):
                setattr(self, k, getattr(FRESH, k))
    h.set_reading(OldReading())
    assert list(h.read("analog-input,1", "status-flags"))[1] == 0
    assert h.read("analog-input,10", "reliability") == Reliability.communicationFailure


# ---- bind re-check (no sockets: drives MeterBacnetServer.check_bind directly)

def _bare_server(cfg, bind_results):
    from water_bacnet.bacnet import MeterBacnetServer

    async def make():
        srv = MeterBacnetServer(cfg, StubSource(FRESH))
        srv.bound_ip = "127.0.0.1"
        it = iter(bind_results)
        srv.bind_ok = lambda: next(it)
        return srv
    return asyncio.run(make())


def test_bind_recheck_raises_only_after_grace(tmp_path):
    cfg = write_config(tmp_path / "c.toml", '[bacnet]\nbind = "127.0.0.1/32"\n')
    bad = (False, "bound address 127.0.0.1 is no longer assigned to this host")
    srv = _bare_server(cfg, [bad, bad, (True, ""), bad, bad, bad])
    srv.bind_grace_s = 120

    async def go():
        await srv.check_bind(mono=1000)     # goes bad
        await srv.check_bind(mono=1060)     # 60 s: still tolerated
        await srv.check_bind(mono=1100)     # recovered: timer reset
        await srv.check_bind(mono=1200)     # bad again
        await srv.check_bind(mono=1300)     # 100 s
        with pytest.raises(RuntimeError, match="no longer assigned"):
            await srv.check_bind(mono=1321)  # > 120 s
    asyncio.run(go())


def test_bind_ok_detects_auto_moving(tmp_path, monkeypatch):
    from water_bacnet import bacnet as B
    cfg = write_config(tmp_path / "c.toml", '[bacnet]\nbind = "auto"\n')

    async def make():
        return B.MeterBacnetServer(cfg, StubSource(FRESH))
    srv = asyncio.run(make())
    srv.bound_ip = "127.0.0.1"
    monkeypatch.setattr(B, "resolve_bind", lambda b: ("127.0.0.1", 8))
    assert srv.bind_ok() == (True, "")
    monkeypatch.setattr(B, "resolve_bind", lambda b: ("198.51.100.7", 24))
    ok, why = srv.bind_ok()
    assert not ok and "198.51.100.7" in why

    def no_route(b):
        raise RuntimeError("no default route")
    monkeypatch.setattr(B, "resolve_bind", no_route)
    assert srv.bind_ok()[0] is True  # still have our address: keep serving
    srv.bound_ip = "192.0.2.123"  # TEST-NET, never local
    assert srv.bind_ok()[0] is False
    assert B.address_is_local("127.0.0.1") and not B.address_is_local("192.0.2.123")
