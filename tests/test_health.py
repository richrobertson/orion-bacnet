"""water_bacnet.health: CPU temp, throttle flags, SHT31/BME280 over a fake i2c bus."""

import asyncio
import errno
import logging
import struct
import subprocess
from types import SimpleNamespace

import pytest

EREMOTEIO = getattr(errno, "EREMOTEIO", 121)  # Linux value; macOS lacks it

from water_bacnet import health as H
from water_bacnet.config import HealthConfig


# ---------------------------------------------------------------------------------
# CPU temperature

def test_cpu_temp_millidegrees(tmp_path):
    p = tmp_path / "temp"
    p.write_text("48312\n")
    assert H.cpu_temp(str(p)) == pytest.approx(48.312)
    p.write_text("-5000\n")
    assert H.cpu_temp(str(p)) == pytest.approx(-5.0)


@pytest.mark.parametrize("text", ["", "hot", "999999999"])
def test_cpu_temp_garbage_raises(tmp_path, text):
    p = tmp_path / "temp"
    p.write_text(text)
    with pytest.raises(ValueError):
        H.cpu_temp(str(p))


def test_cpu_temp_missing_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        H.cpu_temp(str(tmp_path / "nope"))


# ---------------------------------------------------------------------------------
# throttled

@pytest.mark.parametrize("text, value, now, active", [
    ("throttled=0x50005\n", 0x50005, 0x5, True),   # under-voltage + throttled, now & since boot
    ("throttled=0x0\n", 0, 0, False),
    ("throttled=0x50000", 0x50000, 0, False),       # happened since boot, fine now
    ("throttled=0x8", 8, 8, True),                  # soft temperature limit
    ("0x2", 2, 2, True),
    ("327685", 0x50005, 5, True),                   # decimal
    ("  throttled = 0X50005 ", 0x50005, 5, True),
])
def test_parse_throttled(text, value, now, active):
    v = H.parse_throttled(text)
    assert v == value
    assert H.now_bits(v) == now
    assert H.any_now(v) is active


@pytest.mark.parametrize("text", ["", "throttled=", "garbage", "throttled=0xZZ",
                                  "volts=0x5", "throttled=-1", "0x1ffffffff",
                                  "VCHI initialization failed"])
def test_parse_throttled_garbage(text):
    with pytest.raises(ValueError):
        H.parse_throttled(text)


def test_describe_throttled():
    assert "under-voltage" in H.describe_throttled(0x1)
    assert H.describe_throttled(0) == "ok"


def _runner(stdout="throttled=0x0\n", rc=0, exc=None):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if exc is not None:
            raise exc
        return SimpleNamespace(returncode=rc, stdout=stdout, stderr="err")
    run.calls = calls
    return run


def test_read_throttled_runs_vcgencmd():
    run = _runner("throttled=0x50005\n")
    assert H.read_throttled("/usr/bin/vcgencmd", run=run) == 0x50005
    assert run.calls == [["/usr/bin/vcgencmd", "get_throttled"]]
    with pytest.raises(RuntimeError):
        H.read_throttled("vcgencmd", run=_runner(rc=255))


# ---------------------------------------------------------------------------------
# SHT31

def test_crc8_datasheet_vector():
    # Sensirion SHT3x datasheet, section 4.12: CRC(0xBEEF) = 0x92
    assert H.crc8(b"\xbe\xef") == 0x92
    assert H.crc8(b"\x00\x00") == 0x81


class FakeSHT31:
    def __init__(self, raw_t, raw_h, corrupt=False):
        self.address = 0x44
        self.raw_t, self.raw_h, self.corrupt = raw_t, raw_h, corrupt
        self.writes, self.closed, self._buf = [], False, b""

    def write(self, data):
        self.writes.append(bytes(data))
        if bytes(data) == b"\x24\x00":
            t = struct.pack(">H", self.raw_t)
            h = struct.pack(">H", self.raw_h)
            ct = H.crc8(t) ^ (0xFF if self.corrupt else 0)
            self._buf = t + bytes([ct]) + h + bytes([H.crc8(h)])

    def read(self, n):
        out, self._buf = self._buf[:n], self._buf[n:]
        if len(out) != n:
            raise OSError(EREMOTEIO, "Remote I/O error")
        return out

    def read_reg(self, reg, n):
        self.write(bytes([reg]))
        return self.read(n)

    def close(self):
        self.closed = True


def _raw_for(t_c, rh):
    return round((t_c + 45) * 65535 / 175), round(rh * 65535 / 100)


def test_sht31_measure_and_crc():
    dev = FakeSHT31(*_raw_for(21.5, 63.0))
    t, h = H.sht31_measure(dev, sleep=lambda s: None)
    assert dev.writes == [b"\x24\x00"]
    assert t == pytest.approx(21.5, abs=0.01) and h == pytest.approx(63.0, abs=0.01)
    with pytest.raises(ValueError, match="CRC"):
        H.sht31_measure(FakeSHT31(*_raw_for(21.5, 63.0), corrupt=True), sleep=lambda s: None)


# ---------------------------------------------------------------------------------
# BME280

# BMP280/BME280 datasheet temperature example (identical T compensation):
# dig_T1=27504, dig_T2=26435, dig_T3=-1000, adc_T=519888 -> t_fine=128422, T=25.08 C.
DS_CAL = {"T1": 27504, "T2": 26435, "T3": -1000,
          # typical production humidity trims (from a real part)
          "H1": 75, "H2": 362, "H3": 0, "H4": 324, "H5": 50, "H6": 30}


def test_bme280_temperature_datasheet_vector():
    t, t_fine = H.bme280_compensate_t(519888, DS_CAL)
    assert t_fine == 128422
    assert t == pytest.approx(25.08)


def _float_humidity(adc_h, t_fine, c):
    """BME280 datasheet section 8.1 double-precision formula (independent check)."""
    v = t_fine - 76800.0
    v = (adc_h - (c["H4"] * 64.0 + c["H5"] / 16384.0 * v)) * (
        c["H2"] / 65536.0 * (1.0 + c["H6"] / 67108864.0 * v * (1.0 + c["H3"] / 67108864.0 * v)))
    v = v * (1.0 - c["H1"] * v / 524288.0)
    return max(0.0, min(100.0, v))


@pytest.mark.parametrize("adc_h", [20000, 26000, 30000, 34000, 40000])
def test_bme280_humidity_int_matches_float_formula(adc_h):
    _, t_fine = H.bme280_compensate_t(519888, DS_CAL)
    got = H.bme280_compensate_h(adc_h, t_fine, DS_CAL)
    assert 0.0 <= got <= 100.0
    assert got == pytest.approx(_float_humidity(adc_h, t_fine, DS_CAL), abs=0.05)


def test_bme280_humidity_clamps():
    _, t_fine = H.bme280_compensate_t(519888, DS_CAL)
    assert H.bme280_compensate_h(0, t_fine, DS_CAL) == 0.0
    assert H.bme280_compensate_h(65535, t_fine, DS_CAL) == 100.0


def _cal_blocks(c):
    b1 = struct.pack("<HhH", c["T1"], c["T2"], c["T3"] & 0xFFFF) + bytes(18) + b"\x00" + bytes([c["H1"]])
    h4, h5 = c["H4"], c["H5"]
    b2 = struct.pack("<h", c["H2"]) + bytes([c["H3"], (h4 >> 4) & 0xFF,
                                              (h4 & 0x0F) | ((h5 & 0x0F) << 4),
                                              (h5 >> 4) & 0xFF, c["H6"] & 0xFF])
    return b1, b2


def test_bme280_calibration_unpacking_incl_negative_h4_h5():
    c = dict(DS_CAL, H4=-200, H5=-37, H6=-5, T2=-3)
    assert H.bme280_calibration(*_cal_blocks(c)) == c


class FakeBME280:
    def __init__(self, address=0x76, cal=DS_CAL, adc_t=519888, adc_h=30000, chip=0x60):
        self.address = address
        self.regs = bytearray(256)
        self.regs[0xD0] = chip
        b1, b2 = _cal_blocks(cal)
        self.regs[0x88:0x88 + 26] = b1
        self.regs[0xE1:0xE1 + 7] = b2
        self.regs[0xFA] = (adc_t >> 12) & 0xFF
        self.regs[0xFB] = (adc_t >> 4) & 0xFF
        self.regs[0xFC] = (adc_t & 0xF) << 4
        self.regs[0xFD], self.regs[0xFE] = adc_h >> 8, adc_h & 0xFF
        self.ptr, self.writes, self.closed = 0, [], False

    def write(self, data):
        data = bytes(data)
        self.writes.append(data)
        self.ptr = data[0]
        for i, b in enumerate(data[1:]):
            self.regs[self.ptr + i] = b

    def read(self, n):
        return bytes(self.regs[self.ptr:self.ptr + n])

    def read_reg(self, reg, n):
        self.write(bytes([reg]))
        return self.read(n)

    def close(self):
        self.closed = True


def test_bme280_forced_measurement():
    dev = FakeBME280()
    cal = H.bme280_read_calibration(dev)
    t, h = H.bme280_measure(dev, cal, sleep=lambda s: None)
    assert t == pytest.approx(25.08)
    _, t_fine = H.bme280_compensate_t(519888, DS_CAL)
    assert h == pytest.approx(_float_humidity(30000, t_fine, DS_CAL), abs=0.05)
    assert b"\xF2\x01" in dev.writes and b"\xF4\x25" in dev.writes
    assert dev.writes.index(b"\xF2\x01") < dev.writes.index(b"\xF4\x25")  # ctrl_hum first
    assert dev.regs[0xF4] & 0x03 == 0x01  # forced mode


def test_bme280_wrong_chip_id():
    with pytest.raises(LookupError, match="BMP280"):
        H.bme280_read_calibration(FakeBME280(chip=0x58))


# ---------------------------------------------------------------------------------
# probing over a fake bus

class FakeBus:
    """opener(bus, address) for EnclosureSensor; addresses not in `devices` NACK."""

    def __init__(self, devices=None, missing_bus=False):
        self.devices = devices or {}
        self.missing_bus = missing_bus
        self.opened = []

    def __call__(self, bus, address):
        self.opened.append((bus, address))
        if self.missing_bus:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory")
        dev = self.devices.get(address)
        if dev is None:
            return _Nack(address)
        return dev


class _Nack:
    def __init__(self, address):
        self.address = address

    def write(self, data):
        raise OSError(EREMOTEIO, "Remote I/O error")

    def read(self, n):
        raise OSError(EREMOTEIO, "Remote I/O error")

    def read_reg(self, reg, n):
        self.write(bytes([reg]))

    def close(self):
        pass


def test_auto_probe_finds_sht31_first():
    bus = FakeBus({0x44: FakeSHT31(*_raw_for(10.0, 40.0)), 0x76: FakeBME280()})
    s = H.EnclosureSensor("auto", 1, 0, opener=bus, sleep=lambda x: None)
    t, h, name = s.read()
    assert name == "sht31@0x44" and t == pytest.approx(10.0, abs=0.01)
    assert bus.opened == [(1, 0x44)]


def test_auto_probe_falls_through_to_bme280_at_0x77():
    bus = FakeBus({0x77: FakeBME280(address=0x77)})
    s = H.EnclosureSensor("auto", 1, 0, opener=bus, sleep=lambda x: None)
    t, h, name = s.read()
    assert name == "bme280@0x77" and t == pytest.approx(25.08)
    assert [a for _, a in bus.opened] == [0x44, 0x76, 0x77]
    bus.opened.clear()
    s.read()  # remembered: no re-probe
    assert [a for _, a in bus.opened] == [0x77]


def test_explicit_kind_and_address():
    bus = FakeBus({0x44: FakeSHT31(1, 1), 0x77: FakeBME280(address=0x77)})
    s = H.EnclosureSensor("bme280", 1, 0, opener=bus, sleep=lambda x: None)
    assert s.read()[2] == "bme280@0x77"
    s = H.EnclosureSensor("auto", 1, 0x77, opener=bus, sleep=lambda x: None)
    assert s.candidates() == [("bme280", 0x77)]


def test_absent_sensor_and_missing_bus_raise_lookuperror():
    s = H.EnclosureSensor("auto", 1, 0, opener=FakeBus(), sleep=lambda x: None)
    with pytest.raises(LookupError, match="no sensor"):
        s.read()
    s = H.EnclosureSensor("auto", 1, 0, opener=FakeBus(missing_bus=True), sleep=lambda x: None)
    with pytest.raises(LookupError, match="i2c-1 not present"):
        s.read()
    with pytest.raises(LookupError, match="disabled"):
        H.EnclosureSensor("none", opener=FakeBus()).read()


def test_sensor_that_disappears_is_reprobed():
    bus = FakeBus({0x44: FakeSHT31(*_raw_for(10.0, 40.0))})
    s = H.EnclosureSensor("auto", 1, 0, opener=bus, sleep=lambda x: None)
    s.read()
    del bus.devices[0x44]
    with pytest.raises(LookupError):
        s.read()
    assert s.found is None


def test_real_i2c_device_uses_i2c_slave_ioctl(monkeypatch):
    import fcntl
    calls = []
    monkeypatch.setattr(H.os, "open", lambda path, flags: calls.append(("open", path)) or 99)
    monkeypatch.setattr(fcntl, "ioctl", lambda fd, req, arg: calls.append(("ioctl", fd, req, arg)))
    monkeypatch.setattr(H.os, "write", lambda fd, data: calls.append(("write", bytes(data))) or len(data))
    monkeypatch.setattr(H.os, "read", lambda fd, n: calls.append(("read", n)) or b"\x60"[:n])
    monkeypatch.setattr(H.os, "close", lambda fd: calls.append(("close", fd)))
    dev = H.I2CDevice.open(1, 0x76)
    assert dev.read_reg(0xD0, 1) == b"\x60"
    dev.close()
    assert calls == [("open", "/dev/i2c-1"), ("ioctl", 99, 0x0703, 0x76),
                     ("write", b"\xd0"), ("read", 1), ("close", 99)]
    monkeypatch.setattr(H.os, "read", lambda fd, n: b"")
    with pytest.raises(OSError):
        H.I2CDevice(5, 0x44).read(6)


# ---------------------------------------------------------------------------------
# the monitor

def _cfg(tmp_path, **over):
    temp = tmp_path / "temp"
    temp.write_text("51234\n")
    kw = dict(thermal_path=str(temp), vcgencmd_path="vcgencmd", poll_s=5)
    kw.update(over)
    return HealthConfig(**kw)


def test_monitor_all_present(tmp_path):
    bus = FakeBus({0x44: FakeSHT31(*_raw_for(30.0, 80.0))})
    m = H.HealthMonitor(_cfg(tmp_path), opener=bus, run_cmd=_runner("throttled=0x50005"),
                        sleep=lambda x: None)
    r = m.poll_once()
    assert r.cpu_temp_c == pytest.approx(51.234)
    assert r.throttled == 0x50005 and r.throttled_now is True
    assert r.enclosure_temp_c == pytest.approx(30.0, abs=0.01)
    assert r.enclosure_rh == pytest.approx(80.0, abs=0.01)
    assert r.enclosure_sensor == "sht31@0x44"
    assert m.latest() == r
    d = r.as_dict()
    assert d["throttled_hex"] == "0x50005" and d["throttled_now"] is True


def test_monitor_all_absent_never_raises_and_logs_once(tmp_path, caplog):
    cfg = _cfg(tmp_path, thermal_path=str(tmp_path / "missing"))
    m = H.HealthMonitor(cfg, opener=FakeBus(missing_bus=True),
                        run_cmd=_runner(exc=FileNotFoundError(2, "No such file", "vcgencmd")),
                        sleep=lambda x: None)
    caplog.set_level(logging.INFO, logger="water_bacnet.health")
    for _ in range(4):
        r = m.poll_once()
        assert (r.cpu_temp_c, r.throttled, r.throttled_now, r.enclosure_temp_c,
                r.enclosure_rh, r.enclosure_sensor) == (None,) * 6
    msgs = [rec.getMessage() for rec in caplog.records]
    assert len(msgs) == 3, msgs  # one line per sensor, not one per poll
    assert any("CPU temperature unavailable" in x for x in msgs)
    assert any("throttle flags unavailable" in x for x in msgs)
    assert any("enclosure sensor absent" in x for x in msgs)


def test_monitor_logs_state_changes(tmp_path, caplog):
    out = {"v": "throttled=0x0"}
    m = H.HealthMonitor(_cfg(tmp_path, enclosure_sensor="none"), opener=FakeBus(),
                        run_cmd=lambda argv, **kw: SimpleNamespace(returncode=0, stdout=out["v"],
                                                                    stderr=""),
                        sleep=lambda x: None)
    caplog.set_level(logging.INFO, logger="water_bacnet.health")
    m.poll_once()
    out["v"] = "throttled=0x50005"
    m.poll_once()
    m.poll_once()
    out["v"] = "throttled=0x50000"
    m.poll_once()
    thr = [rec for rec in caplog.records if "throttle" in rec.getMessage()]
    assert [rec.levelname for rec in thr] == ["INFO", "WARNING", "INFO"]
    assert "under-voltage" in thr[1].getMessage()
    assert m.latest().throttled_now is False


def test_monitor_garbage_vcgencmd_and_disabled_readers(tmp_path):
    m = H.HealthMonitor(_cfg(tmp_path, cpu_temp=False, enclosure_sensor="none"),
                        opener=FakeBus(), run_cmd=_runner("VCHI initialization failed"))
    r = m.poll_once()
    assert r.cpu_temp_c is None and r.throttled is None
    m = H.HealthMonitor(_cfg(tmp_path, throttled=False), opener=FakeBus(),
                        run_cmd=_runner(exc=AssertionError("must not run")))
    assert m.poll_once().throttled is None
    m = H.HealthMonitor(_cfg(tmp_path), opener=FakeBus(),
                        run_cmd=_runner(exc=subprocess.TimeoutExpired("vcgencmd", 5)))
    assert m.poll_once().throttled is None


def test_monitor_survives_unexpected_exception(tmp_path, caplog):
    def boom(bus, address):
        raise RuntimeError("driver bug")
    m = H.HealthMonitor(_cfg(tmp_path), opener=boom, run_cmd=_runner())
    r = m.poll_once()
    assert r.cpu_temp_c is None and r.updated > 0


def test_latest_goes_unknown_when_polls_stop(tmp_path, monkeypatch):
    m = H.HealthMonitor(_cfg(tmp_path, enclosure_sensor="none"), opener=FakeBus(),
                        run_cmd=_runner())
    m.poll_once()
    assert m.latest().cpu_temp_c is not None
    t = H.time.monotonic() + 3 * m.poll_s + 31
    monkeypatch.setattr(H.time, "monotonic", lambda: t)
    assert m.latest().cpu_temp_c is None


def test_run_polls_in_a_thread(tmp_path):
    m = H.HealthMonitor(_cfg(tmp_path, enclosure_sensor="none"), opener=FakeBus(),
                        run_cmd=_runner("throttled=0x0"))
    m.poll_s = 0.05

    async def go():
        task = asyncio.create_task(m.run())
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(go())
    assert m.latest().throttled == 0


def test_poll_s_has_a_floor():
    assert H.HealthMonitor(HealthConfig(poll_s=0.001)).poll_s == H.MIN_POLL_S
