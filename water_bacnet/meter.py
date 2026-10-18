"""Meter register tracking and the Reading handed to the BACnet layer.

Hard facts that drive this module:
- rtl_433 calls the field `volume_gal`, but for this meter it is CUBIC FEET, 1:1 with
  the register at 1 cu ft resolution. Everything here is cubic feet.
- A flat register for hours is normal (light use). Staleness comes ONLY from packet
  age, never from an unchanged volume.
- ~22 packets/hour is the healthy rate; multi-minute gaps are normal.

Clocks. The Pi has no RTC: at boot the wall clock is whatever fake-hwclock saved, then
NTP steps it, possibly by months. So every *duration* here (packet age, rolling rates,
flow spans, the forward-jump limit) is measured on a monotonic clock, and wall time is
used only for what must survive a restart or be shown to people: persisted
timestamps, `Reading.updated`, history timestamps in state.json.

Callers pass `now` (wall, epoch) and optionally `mono` (time.monotonic()). When `mono`
is omitted it is taken as `now` minus the last seen wall-monotonic offset, i.e. "no
clock step since the previous call"; a caller that never passes `mono` (the tests, a
one-shot --check) therefore gets plain wall-clock arithmetic.

Points restored from state.json only have wall times. They are mapped onto the
monotonic timeline with the current wall-monotonic offset, and re-mapped whenever the
wall clock steps. A restored point that lies in the future of the current wall clock
is discarded; a restored last-packet time in the future means "no packet yet" (stale)
until a live packet arrives. Points heard live keep their monotonic time, so a clock
step never changes their spacing, and their persisted wall time is rebased on the
current offset.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

MODEL = "Badger-ORION"
GALLONS_PER_CUFT = 7.480519

FLOW_WINDOW_S = 6 * 3600.0
FLOW_RECENT_WINDOW_S = 3600.0
RATE_WINDOW_S = 3600.0
RATE_MIN_ELAPSED_S = 300.0  # packets_per_hour is None until we have 5 min of data

# Physical ceiling for the bad-decode jump filter: ~160 gpm (= ~1283 cu ft/h), the
# maximum rating of a 1.5-2 in Badger Recordall. A larger meter needs a larger value.
MAX_FLOW_CUFT_H = 1300.0
JUMP_SLACK_CUFT = 10.0
# A rejected value becomes the new baseline once this many consecutive, mutually
# consistent packets spanning at least REBASELINE_SPAN_S agree on it.
REBASELINE_PACKETS = 3
REBASELINE_SPAN_S = 600.0

CLOCK_STEP_S = 2.0  # wall-monotonic offset change treated as a clock step
CLOCK_STEP_LOG_S = 60.0  # ...and logged as a WARNING above this
FUTURE_TOLERANCE_S = 5.0

RECOVERY_STEPS = ("none", "restart", "usb_reset", "power_cycle", "reboot", "exhausted")


@dataclass
class Reading:
    volume_cuft: Optional[float]
    flow_per_hour: Optional[float]  # cu ft per hour
    flow_recent_per_hour: Optional[float]  # cu ft per hour
    seconds_since_packet: Optional[float]
    snr: Optional[float]
    rssi: Optional[float]
    freq: Optional[float]
    packets_per_hour: Optional[float]
    rtl433_restarts: int
    packets_total: int
    stale: bool
    error: Optional[str]
    # Epoch of the underlying data: last packet (rtl_433 mode) or the state file's
    # `updated` (state_file mode). 0.0 when nothing has ever been received.
    updated: float
    # Recovery ladder (rtl_433 mode). Counters are attempts, persisted in state.json.
    usb_resets: int = 0
    power_cycles: int = 0
    reboots_requested: int = 0
    recovery_step: str = "none"  # none|restart|usb_reset|power_cycle|reboot|exhausted

    @classmethod
    def empty(cls, error: Optional[str] = None) -> "Reading":
        return cls(None, None, None, None, None, None, None, None, 0, 0, True, error, 0.0)


def _number(value) -> Optional[float]:
    """A finite float from a JSON value, or None (bools are not numbers here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        f = float(value)
    except (OverflowError, ValueError):  # a huge JSON integer
        return None
    return f if math.isfinite(f) else None


def _count(value, default: int = 0) -> int:
    """A non-negative int counter from a JSON value; anything odd gives `default`."""
    f = _number(value)
    if f is None or f < 0:
        return default
    return int(f)


def iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def parse_iso(text: str) -> float:
    """ISO8601 -> epoch. Naive timestamps are taken as UTC."""
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class MeterTracker:
    def __init__(self, meter_id: int, history_hours: float = 48):
        self.meter_id = int(meter_id)
        self.history_hours = float(history_hours)
        self.volume: Optional[float] = None
        self.snr: Optional[float] = None
        self.rssi: Optional[float] = None
        self.freq: Optional[float] = None
        self.packets_total = 0
        self.packets_dropped = 0  # valid packets from our id that were not accepted
        self.rtl433_restarts = 0  # incremented by Rtl433Source
        # Recovery ladder state, owned by Rtl433Source, persisted here.
        self.usb_resets = 0
        self.power_cycles = 0
        self.reboots_requested = 0
        self.recovery_step = "none"
        self.recovery_backoff_s: Optional[float] = None
        self.recovery_next_retry: Optional[float] = None  # wall epoch
        # What the last ingest() did: accepted | rejected (valid, ours, dropped) |
        # invalid (ours, unusable volume) | foreign.
        self.last_result = "foreign"
        self.last_heard_mono: Optional[float] = None  # any packet from our meter id

        # [mono, volume, restored_wall_or_None], appended only on change.
        self._hist: list = []
        # Last ACCEPTED packet: monotonic time, plus its wall time while it is a
        # restored (not live) value.
        self._lp_mono: Optional[float] = None
        self._lp_wall: Optional[float] = None
        self._lp_future = False  # restored last packet was in the future
        self._offset: Optional[float] = None  # wall - monotonic
        self._recent: deque = deque()  # accept times (mono) within the rate window
        self._started: Optional[float] = None  # mono of first call
        self._cand: list = []  # rejected-but-consistent packets [(mono, volume)]

    # ---- clock ------------------------------------------------------------------

    def _clock(self, now: Optional[float], mono: Optional[float]) -> tuple:
        """Normalise (now, mono) and react to wall clock steps. Returns (now, mono)."""
        if now is None:
            now = time.time()
            if mono is None:
                mono = time.monotonic()
        if mono is None:
            mono = now - (self._offset or 0.0)
        off = now - mono
        if self._offset is None:
            self._offset = off
            self._remap_restored(now)
        elif abs(off - self._offset) > CLOCK_STEP_S:
            step = off - self._offset
            lvl = logging.WARNING if abs(step) > CLOCK_STEP_LOG_S else logging.INFO
            log.log(lvl, "wall clock stepped by %+.0fs; ages/rates use the monotonic clock", step)
            self._offset = off
            self._remap_restored(now)
        if self._started is None:
            self._started = mono
        return now, mono

    def _remap_restored(self, now: float) -> None:
        """Place restored (wall-only) points on the monotonic timeline; drop future ones."""
        off = self._offset
        kept = []
        dropped = 0
        for p in self._hist:
            wall = p[2]
            if wall is None:
                kept.append(p)
            elif wall > now + FUTURE_TOLERANCE_S:
                dropped += 1
            else:
                p[0] = wall - off
                kept.append(p)
        if dropped:
            log.warning("discarded %d history point(s) dated in the future", dropped)
        kept.sort(key=lambda p: p[0])
        self._hist = kept
        if self._lp_wall is not None:
            if self._lp_wall > now + FUTURE_TOLERANCE_S:
                log.warning(
                    "saved last-packet time %s is in the future; treating the feed as "
                    "stale until a live packet arrives", iso_utc(self._lp_wall))
                self._lp_wall = None
                self._lp_mono = None
                self._lp_future = True
            else:
                self._lp_mono = self._lp_wall - off

    def _wall(self, mono: float) -> float:
        return mono + (self._offset or 0.0)

    @property
    def last_packet(self) -> Optional[float]:
        """Wall epoch of the last accepted packet (None = none / unknown)."""
        if self._lp_wall is not None and self._offset is None:
            return self._lp_wall
        if self._lp_mono is None:
            return None
        return self._lp_wall if self._lp_wall is not None else self._wall(self._lp_mono)

    @property
    def last_packet_mono(self) -> Optional[float]:
        return self._lp_mono

    @property
    def history(self) -> list:
        """[[wall_epoch, volume], ...] oldest first (the persisted form)."""
        out = []
        for m, v, w in self._hist:
            if w is not None:
                out.append([w, v])
            elif m is not None:
                out.append([self._wall(m), v])
        return out

    # ---- ingest -----------------------------------------------------------------

    def ingest(self, pkt: dict, now: Optional[float] = None, mono: Optional[float] = None) -> bool:
        """Feed one decoded rtl_433 JSON object. True if accepted for our meter."""
        now, mono = self._clock(now, mono)
        self.last_result = "foreign"
        if not isinstance(pkt, dict) or pkt.get("model") != MODEL:
            return False
        try:
            if int(pkt.get("id")) != self.meter_id:
                return False
        except (TypeError, ValueError, OverflowError):
            return False

        # Any decode with our model+id proves the radio chain works (ladder input).
        self.last_heard_mono = mono
        self.last_result = "invalid"

        # `volume_gal` is CUBIC FEET for this meter despite the name.
        volume = _number(pkt.get("volume_gal", pkt.get("volume")))
        if volume is None or volume < 0:
            log.warning("dropping packet with invalid volume %r", pkt.get("volume_gal"))
            return False

        if self.volume is not None:
            if self._lp_mono is not None:
                elapsed = mono - self._lp_mono
            else:  # last packet unknown (restored in the future): only this run counts
                elapsed = mono - self._started
            max_rise = MAX_FLOW_CUFT_H * max(0.0, elapsed) / 3600.0 + JUMP_SLACK_CUFT
            reason = None
            if volume > self.volume + max_rise:
                reason = (f"register jumped {self.volume:.0f} -> {volume:.0f} cu ft in "
                          f"{elapsed:.0f}s (max plausible +{max_rise:.0f})")
            elif volume < self.volume - 1:
                # The register is monotonic; a big step backwards is a bad decode.
                reason = f"register went backwards {self.volume:.0f} -> {volume:.0f} cu ft"
            if reason is not None:
                if self._candidate(volume, mono):
                    log.warning(
                        "register RE-BASELINED %.0f -> %.0f cu ft: %d consecutive packets "
                        "over %.0fs agree (the old value was a bad decode or the meter "
                        "was changed); flow history restarted",
                        self.volume, volume, len(self._cand), self._cand[-1][0] - self._cand[0][0])
                    self._cand = []
                    self._hist = []
                    self.volume = None  # fall through to accept as a fresh baseline
                else:
                    self.packets_dropped += 1
                    self.last_result = "rejected"
                    lvl = logging.WARNING if len(self._cand) <= 1 else logging.INFO
                    log.log(lvl, "dropping packet: %s", reason)
                    return False

        # A 1 cu ft dip is decode jitter: count the packet but hold the higher reading,
        # so the published register never runs backwards.
        if self.volume is not None and volume < self.volume:
            volume = self.volume

        self._cand = []
        self.volume = volume
        self.snr = _number(pkt.get("snr"))
        self.rssi = _number(pkt.get("rssi"))
        freq = _number(pkt.get("freq1"))
        self.freq = freq if freq is not None else _number(pkt.get("freq"))
        self._lp_mono = mono
        self._lp_wall = None
        self._lp_future = False
        self.packets_total += 1
        self._recent.append(mono)
        self.last_result = "accepted"

        if not self._hist or self._hist[-1][1] != volume:
            self._hist.append([mono, volume, None])
        self._trim(mono)
        return True

    def _candidate(self, volume: float, mono: float) -> bool:
        """Track a rejected value; True when enough packets agree to re-baseline."""
        c = self._cand
        if c:
            t, v = c[-1]
            rise = MAX_FLOW_CUFT_H * max(0.0, mono - t) / 3600.0 + JUMP_SLACK_CUFT
            if not (v - 1 <= volume <= v + rise):
                c = []
        c.append((mono, volume))
        self._cand = c
        return len(c) >= REBASELINE_PACKETS and c[-1][0] - c[0][0] >= REBASELINE_SPAN_S

    def _trim(self, mono: float) -> None:
        # Drop history older than history_hours, but keep the newest point at or before
        # the cutoff: it is the register value at the start of the kept span.
        cutoff = mono - self.history_hours * 3600.0
        while len(self._hist) >= 2 and self._hist[1][0] <= cutoff:
            self._hist.pop(0)
        while self._recent and self._recent[0] <= mono - RATE_WINDOW_S:
            self._recent.popleft()

    # ---- derived values ---------------------------------------------------------

    def _flow(self, window_s: float) -> Optional[float]:
        """Register delta over the window ending at the last packet, per hour.

        Uses monotonic times only, and is clamped to [0, MAX_FLOW_CUFT_H].
        """
        if self.volume is None or self._lp_mono is None:
            return None
        pts = [p for p in self._hist if p[0] is not None and p[0] <= self._lp_mono]
        if not pts:
            return None
        end = self._lp_mono
        start = end - window_s
        before = [p for p in pts if p[0] <= start]
        if before:
            # The register held this value at the window start, so the span is the
            # full window.
            base_volume = before[-1][1]
            elapsed = window_s
        else:
            # Not enough history yet: use the oldest point we have.
            base_t, base_volume = pts[0][0], pts[0][1]
            elapsed = end - base_t
            if elapsed <= 0:
                return None
        delta = self.volume - base_volume
        return min(MAX_FLOW_CUFT_H, max(0.0, delta * 3600.0 / elapsed))

    def _packets_per_hour(self, mono: float) -> Optional[float]:
        if self._started is None:
            return None
        while self._recent and self._recent[0] <= mono - RATE_WINDOW_S:
            self._recent.popleft()
        elapsed = mono - self._started
        if elapsed < RATE_MIN_ELAPSED_S:
            return None
        count = len(self._recent)
        if elapsed < RATE_WINDOW_S:
            return count * RATE_WINDOW_S / elapsed
        return float(count)

    def reading(self, now: Optional[float] = None, stale_after: float = 900.0,
                mono: Optional[float] = None) -> Reading:
        now, mono = self._clock(now, mono)
        age = None if self._lp_mono is None else max(0.0, mono - self._lp_mono)
        if age is None:
            if self._lp_future:
                stale, error = True, "saved last-packet time is in the future (clock step?); waiting for a live packet"
            else:
                stale, error = True, "no packet received yet"
        elif age > stale_after:
            stale, error = True, f"no packet for {age:.0f}s"
        else:
            stale, error = False, None
        lp = self.last_packet
        return Reading(
            volume_cuft=self.volume,
            flow_per_hour=self._flow(FLOW_WINDOW_S),
            flow_recent_per_hour=self._flow(FLOW_RECENT_WINDOW_S),
            seconds_since_packet=age,
            snr=self.snr,
            rssi=self.rssi,
            freq=self.freq,
            packets_per_hour=self._packets_per_hour(mono),
            rtl433_restarts=self.rtl433_restarts,
            packets_total=self.packets_total,
            stale=stale,
            error=error,
            updated=lp if lp is not None else 0.0,
            usb_resets=self.usb_resets,
            power_cycles=self.power_cycles,
            reboots_requested=self.reboots_requested,
            recovery_step=self.recovery_step,
        )

    # ---- persistence (same key names as the standalone collector's state.json) ----

    def to_json(self, now: Optional[float] = None, mono: Optional[float] = None) -> dict:
        now, mono = self._clock(now, mono)
        r = self.reading(now, float("inf"), mono)
        return {
            "updated": iso_utc(now),
            "meter_id": self.meter_id,
            "volume": self.volume,
            "volume_units": "cubic_feet",
            "flow_per_hour": r.flow_per_hour,
            "flow_recent_per_hour": r.flow_recent_per_hour,
            # as of `updated`
            "seconds_since_packet": r.seconds_since_packet,
            "snr": self.snr,
            "rssi": self.rssi,
            "freq": self.freq,
            "packets_total": self.packets_total,
            "packets_per_hour": r.packets_per_hour,
            "rtl433_restarts": self.rtl433_restarts,
            "usb_resets": self.usb_resets,
            "power_cycles": self.power_cycles,
            "reboots_requested": self.reboots_requested,
            "recovery_step": self.recovery_step,
            "recovery_backoff_s": self.recovery_backoff_s,
            "recovery_next_retry": self.recovery_next_retry,
            "history": self.history,
        }

    @classmethod
    def from_json(
        cls, d: dict, meter_id: Optional[int] = None, history_hours: float = 48
    ) -> "MeterTracker":
        """Rebuild from to_json() output (or a standalone collector's state.json).

        Derived values (flows, packets_per_hour) are recomputed, not restored; the
        rolling packet count restarts from zero.
        """
        if not isinstance(d, dict):
            raise ValueError("state is not a JSON object")
        mid = meter_id if meter_id is not None else d.get("meter_id")
        if mid is None:
            raise ValueError("state has no meter_id")
        t = cls(int(mid), history_hours)
        t.restore(d)
        return t

    def restore(self, d: dict) -> None:
        """Load persisted state into this tracker in place.

        Raises ValueError/TypeError on a structurally broken document; individual odd
        values (null, 1e400, strings) are ignored rather than fatal.
        """
        if not isinstance(d, dict):
            raise ValueError("state is not a JSON object")
        updated = parse_iso(d["updated"]) if d.get("updated") else None
        if updated is not None and not math.isfinite(updated):
            updated = None
        self.volume = _number(d.get("volume"))
        if self.volume is not None and self.volume < 0:
            self.volume = None
        self.snr = _number(d.get("snr"))
        self.rssi = _number(d.get("rssi"))
        self.freq = _number(d.get("freq"))
        self.packets_total = _count(d.get("packets_total"))
        self.rtl433_restarts = _count(d.get("rtl433_restarts"))
        self.usb_resets = _count(d.get("usb_resets"))
        self.power_cycles = _count(d.get("power_cycles"))
        self.reboots_requested = _count(d.get("reboots_requested"))
        step = d.get("recovery_step")
        self.recovery_step = step if step in RECOVERY_STEPS else "none"
        self.recovery_backoff_s = _number(d.get("recovery_backoff_s"))
        self.recovery_next_retry = _number(d.get("recovery_next_retry"))

        ssp = _number(d.get("seconds_since_packet"))
        lp = updated - ssp if (updated is not None and ssp is not None) else None
        self._lp_wall = lp
        self._lp_mono = None
        self._lp_future = False

        hist = []
        raw = d.get("history")
        for item in raw if isinstance(raw, list) else []:
            try:
                ts, vol = _number(item[0]), _number(item[1])
            except (TypeError, IndexError, KeyError):
                continue
            if ts is None or vol is None:
                continue
            hist.append([None, vol, ts])
        hist.sort(key=lambda p: p[2])
        if self.volume is not None and (not hist or hist[-1][1] != self.volume):
            if lp is not None:
                hist.append([None, self.volume, lp])
        self._hist = hist
        self._cand = []
        # Everything time-bearing was just replaced: anchor the restored wall times on
        # the next call's (now, mono).
        self._offset = None
