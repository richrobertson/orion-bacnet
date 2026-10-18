"""Health of the box itself: CPU temperature, firmware throttle flags, and an optional
I2C enclosure sensor (SHT31 or BME280). Published as BACnet AI12-AI15 and BI2.

Every reader is optional and fakeable:
  * a missing sensor gives None (BACnet reliability noSensor), never an exception into
    the event loop;
  * each sensor's state is logged ONCE per change (absent -> ok -> failing ...), so a Pi
    without an enclosure sensor does not fill the journal every minute;
  * all blocking I/O (sysfs, vcgencmd, /dev/i2c-N) runs in a worker thread via
    asyncio.to_thread, every [health] poll_s.

Stdlib only: I2C uses the Linux i2c-dev interface directly (open /dev/i2c-N, ioctl
I2C_SLAVE, plain read()/write()), no smbus module.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

log = logging.getLogger(__name__)

# linux/i2c-dev.h
I2C_SLAVE = 0x0703

SHT31_ADDRESSES = (0x44, 0x45)
BME280_ADDRESSES = (0x76, 0x77)
BME280_CHIP_ID = 0x60

# vcgencmd get_throttled bits (Raspberry Pi firmware). 0-3 = happening NOW,
# 16-19 = has happened since boot.
THROTTLE_BITS = {
    0: "under-voltage", 1: "arm frequency capped", 2: "throttled", 3: "soft temperature limit",
    16: "under-voltage has occurred", 17: "frequency capping has occurred",
    18: "throttling has occurred", 19: "soft temperature limit has occurred",
}

MIN_POLL_S = 5.0


# ---------------------------------------------------------------------------------
# CPU temperature and throttle flags

def cpu_temp(path: str = "/sys/class/thermal/thermal_zone0/temp") -> float:
    """SoC temperature in degrees C from a sysfs thermal zone (millidegrees)."""
    with open(path, "r") as fh:
        raw = fh.read().strip()
    value = int(raw) / 1000.0
    if not -60.0 <= value <= 150.0:
        raise ValueError(f"implausible CPU temperature {value} C from {path}")
    return value


def parse_throttled(text: str) -> int:
    """`vcgencmd get_throttled` output -> int. Accepts "throttled=0x50005", "0x50005"
    or a decimal number. Raises ValueError on anything else."""
    s = (text or "").strip()
    if "=" in s:
        key, _, s = s.partition("=")
        if key.strip() != "throttled":
            raise ValueError(f"unexpected vcgencmd output {text!r}")
        s = s.strip()
    if not s:
        raise ValueError(f"unexpected vcgencmd output {text!r}")
    try:
        v = int(s, 16) if s.lower().startswith("0x") else int(s, 10)
    except ValueError:
        raise ValueError(f"unexpected vcgencmd output {text!r}") from None
    if not 0 <= v <= 0xFFFFFFFF:
        raise ValueError(f"throttled value out of range: {text!r}")
    return v


def now_bits(v: int) -> int:
    """Bits 0-3: under-voltage / capped / throttled / soft temp limit, right NOW."""
    return v & 0xF


def any_now(v: int) -> bool:
    return now_bits(v) != 0


def describe_throttled(v: int) -> str:
    names = [name for bit, name in sorted(THROTTLE_BITS.items()) if v & (1 << bit)]
    return ", ".join(names) or "ok"


def read_throttled(vcgencmd_path: str = "vcgencmd",
                   run: Callable[..., Any] = subprocess.run) -> int:
    p = run([vcgencmd_path, "get_throttled"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=5)
    if p.returncode != 0:
        raise RuntimeError(f"vcgencmd get_throttled exited {p.returncode}: "
                           f"{(p.stderr or p.stdout or '').strip()[:200]}")
    return parse_throttled(p.stdout)


# ---------------------------------------------------------------------------------
# I2C (Linux i2c-dev)

class I2CDevice:
    """One 7-bit address on /dev/i2c-<bus>. Plain write()/read() transactions."""

    def __init__(self, fd: int, address: int):
        self.fd = fd
        self.address = address

    @classmethod
    def open(cls, bus: int, address: int) -> "I2CDevice":
        import fcntl  # Linux/macOS only; imported lazily so the module loads anywhere

        fd = os.open(f"/dev/i2c-{int(bus)}", os.O_RDWR)
        try:
            fcntl.ioctl(fd, I2C_SLAVE, address)
        except OSError:
            os.close(fd)
            raise
        return cls(fd, address)

    def write(self, data: bytes) -> None:
        n = os.write(self.fd, bytes(data))
        if n != len(data):
            raise OSError(errno.EIO, f"short I2C write to 0x{self.address:02x}")

    def read(self, n: int) -> bytes:
        data = os.read(self.fd, n)
        if len(data) != n:
            raise OSError(errno.EIO, f"short I2C read from 0x{self.address:02x}")
        return data

    def read_reg(self, reg: int, n: int) -> bytes:
        self.write(bytes([reg]))
        return self.read(n)

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


Opener = Callable[[int, int], Any]  # (bus, address) -> device with write/read/read_reg/close


def crc8(data: bytes) -> int:
    """Sensirion CRC-8: polynomial 0x31, init 0xFF, no reflection, no final XOR."""
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def sht31_measure(dev, sleep: Callable[[float], None] = time.sleep) -> Tuple[float, float]:
    """Single shot, high repeatability, no clock stretching (0x2400). -> (C, %RH)."""
    dev.write(b"\x24\x00")
    sleep(0.02)  # max 15.5 ms
    d = dev.read(6)
    if crc8(d[0:2]) != d[2]:
        raise ValueError(f"SHT31 temperature CRC mismatch ({d[0:3].hex()})")
    if crc8(d[3:5]) != d[5]:
        raise ValueError(f"SHT31 humidity CRC mismatch ({d[3:6].hex()})")
    raw_t = (d[0] << 8) | d[1]
    raw_h = (d[3] << 8) | d[4]
    t = -45.0 + 175.0 * raw_t / 65535.0
    h = 100.0 * raw_h / 65535.0
    return t, min(100.0, max(0.0, h))


# ---- BME280 (Bosch datasheet BST-BME280-DS002, section 4.2.3 / 8.2 integer code)

def _u16(b: bytes, i: int) -> int:
    return b[i] | (b[i + 1] << 8)


def _s16(b: bytes, i: int) -> int:
    v = _u16(b, i)
    return v - 0x10000 if v & 0x8000 else v


def _s8(v: int) -> int:
    return v - 0x100 if v & 0x80 else v


def bme280_calibration(block1: bytes, block2: bytes) -> Dict[str, int]:
    """block1 = 26 bytes from 0x88..0xA1, block2 = 7 bytes from 0xE1..0xE7."""
    if len(block1) < 26 or len(block2) < 7:
        raise ValueError("short BME280 calibration read")
    e4, e5, e6 = block2[3], block2[4], block2[5]
    return {
        "T1": _u16(block1, 0), "T2": _s16(block1, 2), "T3": _s16(block1, 4),
        "H1": block1[25],
        "H2": _s16(block2, 0),
        "H3": block2[2],
        # H4/H5 are 12-bit signed, split across 0xE4/0xE5/0xE6 (E4, E6 are signed).
        "H4": (_s8(e4) * 16) | (e5 & 0x0F),
        "H5": (_s8(e6) * 16) | (e5 >> 4),
        "H6": _s8(block2[6]),
    }


def bme280_compensate_t(adc_t: int, cal: Dict[str, int]) -> Tuple[float, int]:
    """Bosch BME280_compensate_T_int32 -> (degrees C, t_fine)."""
    t1, t2, t3 = cal["T1"], cal["T2"], cal["T3"]
    var1 = (((adc_t >> 3) - (t1 << 1)) * t2) >> 11
    var2 = (((((adc_t >> 4) - t1) * ((adc_t >> 4) - t1)) >> 12) * t3) >> 14
    t_fine = var1 + var2
    t = (t_fine * 5 + 128) >> 8  # 0.01 C
    return t / 100.0, t_fine


def bme280_compensate_h(adc_h: int, t_fine: int, cal: Dict[str, int]) -> float:
    """Bosch bme280_compensate_H_int32 -> %RH (Q22.10 / 1024)."""
    h1, h2, h3, h4, h5, h6 = (cal[k] for k in ("H1", "H2", "H3", "H4", "H5", "H6"))
    v = t_fine - 76800
    v = (((((adc_h << 14) - (h4 << 20) - (h5 * v)) + 16384) >> 15)
         * (((((((v * h6) >> 10) * (((v * h3) >> 11) + 32768)) >> 10) + 2097152) * h2
             + 8192) >> 14))
    v = v - (((((v >> 15) * (v >> 15)) >> 7) * h1) >> 4)
    v = max(0, min(v, 419430400))
    return (v >> 12) / 1024.0


def bme280_read_calibration(dev) -> Dict[str, int]:
    chip = dev.read_reg(0xD0, 1)[0]
    if chip != BME280_CHIP_ID:
        raise LookupError(f"chip id 0x{chip:02x} at 0x{dev.address:02x} is not a BME280 (0x60)"
                          + (" -- a BMP280 has no humidity sensor" if chip in (0x56, 0x57, 0x58)
                             else ""))
    return bme280_calibration(dev.read_reg(0x88, 26), dev.read_reg(0xE1, 7))


def bme280_measure(dev, cal: Dict[str, int],
                   sleep: Callable[[float], None] = time.sleep) -> Tuple[float, float]:
    """One forced-mode measurement, oversampling x1 -> (C, %RH)."""
    dev.write(b"\xF2\x01")  # ctrl_hum: osrs_h x1 (takes effect on the ctrl_meas write)
    dev.write(b"\xF4\x25")  # ctrl_meas: osrs_t x1, osrs_p x1, forced mode
    sleep(0.012)  # max 9.3 ms at x1/x1/x1
    for _ in range(10):
        if not dev.read_reg(0xF3, 1)[0] & 0x08:  # status.measuring
            break
        sleep(0.005)
    else:
        raise TimeoutError("BME280 measurement did not finish")
    d = dev.read_reg(0xF7, 8)
    adc_t = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
    adc_h = (d[6] << 8) | d[7]
    if adc_t == 0x80000 or adc_h == 0x8000:
        raise ValueError("BME280 returned the 'skipped' marker (measurement not enabled)")
    t, t_fine = bme280_compensate_t(adc_t, cal)
    return t, bme280_compensate_h(adc_h, t_fine, cal)


class EnclosureSensor:
    """Finds and reads the enclosure sensor. read() -> (C, %RH, "sht31@0x44") or raises
    LookupError when nothing answers."""

    def __init__(self, kind: str = "auto", bus: int = 1, address: int = 0,
                 opener: Opener = I2CDevice.open, sleep: Callable[[float], None] = time.sleep):
        kind = (kind or "auto").lower()
        if kind not in ("auto", "sht31", "bme280", "none"):
            log.warning("health: unknown enclosure_sensor %r; probing (auto)", kind)
            kind = "auto"
        self.kind, self.bus, self.address = kind, int(bus), int(address)
        self.opener, self.sleep = opener, sleep
        self.found: Optional[Tuple[str, int]] = None
        self._cal: Dict[int, Dict[str, int]] = {}

    def candidates(self):
        out = []
        if self.kind in ("auto", "sht31"):
            out += [("sht31", a) for a in SHT31_ADDRESSES[:1]]
        if self.kind in ("auto", "bme280"):
            out += [("bme280", a) for a in BME280_ADDRESSES]
        if self.address:
            want = [("sht31", self.address)] if self.address in SHT31_ADDRESSES else []
            want += [("bme280", self.address)] if self.address in BME280_ADDRESSES else []
            out = [c for c in want if self.kind in ("auto", c[0])]
        return out

    def _read(self, kind: str, address: int) -> Tuple[float, float]:
        dev = self.opener(self.bus, address)
        try:
            if kind == "sht31":
                return sht31_measure(dev, self.sleep)
            cal = self._cal.get(address)
            if cal is None:
                cal = self._cal[address] = bme280_read_calibration(dev)
            return bme280_measure(dev, cal, self.sleep)
        finally:
            dev.close()

    def read(self) -> Tuple[float, float, str]:
        if self.kind == "none":
            raise LookupError("disabled (enclosure_sensor = none)")
        if self.found is not None:
            kind, address = self.found
            try:
                t, h = self._read(kind, address)
                return t, h, f"{kind}@0x{address:02x}"
            except (OSError, ValueError, LookupError, TimeoutError):
                self.found = None  # fall through and probe again
                self._cal.pop(address, None)
        errors = []
        for kind, address in self.candidates():
            try:
                t, h = self._read(kind, address)
            except FileNotFoundError as exc:  # no /dev/i2c-N: I2C not enabled
                raise LookupError(f"/dev/i2c-{self.bus} not present (I2C disabled?): "
                                  f"{exc.strerror}") from None
            except (OSError, ValueError, LookupError, TimeoutError) as exc:
                errors.append(f"{kind}@0x{address:02x}: {exc}")
                continue
            self.found = (kind, address)
            return t, h, f"{kind}@0x{address:02x}"
        raise LookupError("no sensor answered on /dev/i2c-%d (%s)" % (self.bus, "; ".join(errors)))


# ---------------------------------------------------------------------------------
# Monitor

@dataclass(frozen=True)
class HealthReading:
    cpu_temp_c: Optional[float] = None
    throttled: Optional[int] = None  # raw get_throttled value
    enclosure_temp_c: Optional[float] = None
    enclosure_rh: Optional[float] = None
    enclosure_sensor: Optional[str] = None  # e.g. "sht31@0x44"
    updated: float = 0.0  # time.monotonic() of the poll, 0 = never polled

    @property
    def throttled_now(self) -> Optional[bool]:
        return None if self.throttled is None else any_now(self.throttled)

    def as_dict(self) -> dict:
        return {"cpu_temp_c": self.cpu_temp_c, "throttled": self.throttled,
                "throttled_hex": None if self.throttled is None else hex(self.throttled),
                "throttled_now": self.throttled_now,
                "enclosure_temp_c": self.enclosure_temp_c, "enclosure_rh": self.enclosure_rh,
                "enclosure_sensor": self.enclosure_sensor}


class HealthMonitor:
    """Polls the health readers. poll_once() is blocking; run() polls forever in a
    worker thread and never raises (except CancelledError)."""

    def __init__(self, cfg: Any = None, *, opener: Opener = I2CDevice.open,
                 run_cmd: Callable[..., Any] = subprocess.run,
                 sleep: Callable[[float], None] = time.sleep):
        from .config import HealthConfig

        self.cfg = cfg if cfg is not None else HealthConfig()
        c = self.cfg
        self.run_cmd = run_cmd
        self.poll_s = max(MIN_POLL_S, float(getattr(c, "poll_s", 60.0)))
        self.enclosure = EnclosureSensor(getattr(c, "enclosure_sensor", "auto"),
                                         getattr(c, "i2c_bus", 1), getattr(c, "i2c_address", 0),
                                         opener=opener, sleep=sleep)
        self._latest = HealthReading()
        self._states: Dict[str, str] = {}

    def _note(self, name: str, state: str, level: int = logging.INFO) -> None:
        """Log `name` only when its state string changes."""
        if self._states.get(name) != state:
            self._states[name] = state
            log.log(level, "%s %s", name, state)

    def _cpu(self) -> Optional[float]:
        if not getattr(self.cfg, "cpu_temp", True):
            self._note("CPU temperature", "disabled in config")
            return None
        path = self.cfg.thermal_path
        try:
            v = cpu_temp(path)
        except (OSError, ValueError) as exc:
            self._note("CPU temperature", f"unavailable ({path}: {exc})", logging.WARNING)
            return None
        self._note("CPU temperature", f"ok ({path})")
        return v

    def _throttled(self) -> Optional[int]:
        if not getattr(self.cfg, "throttled", True):
            self._note("throttle flags", "disabled in config")
            return None
        try:
            v = read_throttled(self.cfg.vcgencmd_path, run=self.run_cmd)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            self._note("throttle flags", f"unavailable ({self.cfg.vcgencmd_path}: {exc})",
                       logging.WARNING)
            return None
        # State includes the NOW bits, so under-voltage starting/stopping is logged once.
        level = logging.WARNING if any_now(v) else logging.INFO
        self._note("throttle flags", f"now: {describe_throttled(now_bits(v))}", level)
        return v

    def _enclosure(self):
        try:
            t, h, name = self.enclosure.read()
        except LookupError as exc:
            level = logging.INFO if self.enclosure.kind == "none" else logging.WARNING
            self._note("enclosure sensor", f"absent: {exc}", level)
            return None, None, None
        self._note("enclosure sensor", f"ok ({name})")
        return t, h, name

    def poll_once(self) -> HealthReading:
        try:
            cpu = self._cpu()
            thr = self._throttled()
            t, h, name = self._enclosure()
            r = HealthReading(cpu, thr, t, h, name, time.monotonic())
        except Exception:  # never let one bad reader take the loop down
            log.exception("health: poll failed")
            r = HealthReading(updated=time.monotonic())
        self._latest = r
        return r

    def latest(self) -> HealthReading:
        """The last poll, or an all-None reading if it is older than 3 poll periods."""
        r = self._latest
        if r.updated and time.monotonic() - r.updated > 3 * self.poll_s + 30:
            return HealthReading(updated=r.updated)
        return r

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.poll_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("health: poll thread failed")
            await asyncio.sleep(self.poll_s)
