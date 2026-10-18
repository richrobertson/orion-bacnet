"""Wall-clock steps must not corrupt ages, staleness or flow (the Pi has no RTC)."""

import json

import pytest

from conftest import METER_ID
from water_bacnet.meter import MAX_FLOW_CUFT_H, MeterTracker, iso_utc

W0 = 1_790_000_000.0  # wall
M0 = 5_000.0  # monotonic (arbitrary, unrelated to wall)
STALE = 900.0
DAY = 86400.0


def pkt(volume):
    return {"model": "Badger-ORION", "id": METER_ID, "volume_gal": volume, "snr": 16.0,
            "rssi": -9.0, "freq1": 916.4}


def feed(t, start_s, end_s, step_s, vol_fn, wall_off=0.0):
    """Packets every step_s on the monotonic clock; wall = W0 + s + wall_off."""
    s = start_s
    while s <= end_s:
        assert t.ingest(pkt(vol_fn(s)), W0 + s + wall_off, M0 + s), s
        s += step_s
    return s - step_s


def sane(r):
    for f in (r.flow_per_hour, r.flow_recent_per_hour):
        assert f is None or 0.0 <= f <= MAX_FLOW_CUFT_H, f
    assert r.seconds_since_packet is None or r.seconds_since_packet >= 0


def test_wall_clock_steps_back_one_day():
    t = MeterTracker(METER_ID)
    # 3 h at 2 cu ft/h, then the wall clock steps back a day; packets continue.
    last = feed(t, 0, 3 * 3600, 120, lambda s: 1000 + int(s / 1800))
    last = feed(t, last + 120, 6 * 3600, 120, lambda s: 1000 + int(s / 1800), wall_off=-DAY)
    now, mono = W0 + last + 30 - DAY, M0 + last + 30
    r = t.reading(now, STALE, mono)
    sane(r)
    assert r.stale is False and r.seconds_since_packet == pytest.approx(30)
    assert r.flow_per_hour == pytest.approx(2.0, rel=0.1)
    assert r.flow_recent_per_hour == pytest.approx(2.0, rel=0.1)
    d = t.to_json(now, mono)
    assert all(ts <= now for ts, _ in d["history"]), "history in the future of now"
    assert d["seconds_since_packet"] == pytest.approx(30)
    # round trip on the corrected clock gives the same picture
    t2 = MeterTracker.from_json(json.loads(json.dumps(d)))
    r2 = t2.reading(now + 10, STALE, 1.0)
    sane(r2)
    assert r2.seconds_since_packet == pytest.approx(40)
    assert r2.flow_per_hour == pytest.approx(r.flow_per_hour, rel=0.05)


def test_wall_clock_jumps_forward_six_months():
    t = MeterTracker(METER_ID)
    last = feed(t, 0, 2 * 3600, 120, lambda s: 500)
    jump = 182 * DAY
    # no packet across the jump: age must be monotonic time, not 6 months
    r = t.reading(W0 + last + 60 + jump, STALE, M0 + last + 60)
    sane(r)
    assert r.stale is False and r.seconds_since_packet == pytest.approx(60)
    last2 = feed(t, last + 120, 4 * 3600, 120, lambda s: 500 + int(s / 3600), wall_off=jump)
    r = t.reading(W0 + last2 + jump, STALE, M0 + last2)
    sane(r)
    assert r.flow_recent_per_hour == pytest.approx(1.0, rel=0.1)
    assert r.updated == pytest.approx(W0 + last2 + jump)
    # staleness still works after the jump
    assert t.reading(W0 + last2 + jump + STALE + 1, STALE, M0 + last2 + STALE + 1).stale


def test_restored_last_packet_in_the_future_is_stale_until_live_packet():
    t = MeterTracker(METER_ID)
    feed(t, 0, 3600, 120, lambda s: 41774)
    d = t.to_json(W0 + 3600, M0 + 3600)
    # Restart with the wall clock a day BEHIND the saved state (fake-hwclock / RTC-less boot).
    now = W0 + 3600 - DAY
    t2 = MeterTracker.from_json(d)
    r = t2.reading(now, STALE, 10.0)
    sane(r)
    assert r.stale is True
    assert r.seconds_since_packet is None
    assert "future" in r.error
    assert r.volume_cuft == 41774  # last good value is still shown
    assert t2.history == [] or all(ts <= now for ts, _ in t2.history)
    assert t2.ingest(pkt(41774), now + 30, 40.0)
    r = t2.reading(now + 40, STALE, 50.0)
    assert r.stale is False and r.seconds_since_packet == pytest.approx(10)


def test_restored_future_history_entries_discarded():
    d = {"updated": iso_utc(W0), "meter_id": METER_ID, "volume": 103.0,
         "seconds_since_packet": 60.0, "packets_total": 5,
         "history": [[W0 - 7200, 100.0], [W0 - 3600, 101.0], [W0 + 3600, 102.0],
                     [W0 + 99 * DAY, 103.0]]}
    t = MeterTracker.from_json(d)
    r = t.reading(W0 + 10, STALE, 1.0)
    sane(r)
    walls = [ts for ts, _ in t.history]
    assert all(ts <= W0 + 10 for ts in walls)
    assert W0 - 7200 in walls and W0 - 3600 in walls
    assert r.seconds_since_packet == pytest.approx(70)


def test_ntp_step_after_restore_makes_restored_data_older():
    """Boot with fake-hwclock (wall behind), restore, then NTP steps forward 3 days."""
    t = MeterTracker(METER_ID)
    feed(t, 0, 6 * 3600, 300, lambda s: 2000 + int(s / 3600))
    d = t.to_json(W0 + 6 * 3600, M0 + 6 * 3600)
    t2 = MeterTracker.from_json(d)
    boot_wall = W0 + 6 * 3600 + 120  # fake-hwclock: just after the last save
    r = t2.reading(boot_wall, STALE, 3.0)
    assert r.stale is False  # looks fresh until the clock is corrected...
    r = t2.reading(boot_wall + 3 * DAY, STALE, 33.0)  # NTP step +3 days
    sane(r)
    assert r.stale is True
    assert r.seconds_since_packet == pytest.approx(3 * DAY + 120, abs=2)
    # a live packet after a 3-day gap with a plausible rise is accepted; flow stays sane
    assert t2.ingest(pkt(2100), boot_wall + 3 * DAY + 60, 93.0)
    sane(t2.reading(boot_wall + 3 * DAY + 61, STALE, 94.0))


def test_flow_is_clamped_to_physical_max():
    t = MeterTracker(METER_ID)
    assert t.ingest(pkt(100), W0, M0)
    assert t.ingest(pkt(108), W0 + 1, M0 + 1)  # within the +10 slack, 1 s apart
    r = t.reading(W0 + 2, STALE, M0 + 2)
    assert r.flow_per_hour == MAX_FLOW_CUFT_H
    assert r.flow_recent_per_hour == MAX_FLOW_CUFT_H


def test_packets_per_hour_uses_monotonic_clock():
    t = MeterTracker(METER_ID)
    s = 0.0
    for i in range(30):  # 20/h for 90 min, wall clock stepping back an hour midway
        wall_off = -3600.0 if i >= 15 else 0.0
        t.ingest(pkt(1), W0 + s + wall_off, M0 + s)
        s += 180
    r = t.reading(W0 + s - 3600, STALE, M0 + s - 179)
    assert r.packets_per_hour == pytest.approx(20, abs=1.5)
