import json

import pytest

from conftest import FIXTURES, METER_ID, load_packets
from water_bacnet.meter import MeterTracker, Reading

T0 = 1_760_000_000.0  # arbitrary epoch
STALE = 900.0


def pkt(volume, id=METER_ID, model="Badger-ORION", snr=16.7, rssi=-8.6, freq1=916.4):
    return {"time": "2026-10-18T10:00:00", "model": model, "id": id, "volume_gal": volume,
            "flags_1": 65, "flags_2": 5, "mic": "CRC", "mod": "FSK", "freq1": freq1,
            "freq2": 916.5, "rssi": rssi, "snr": snr, "noise": -25.3}


def test_reading_has_contract_fields():
    r = MeterTracker(METER_ID).reading(T0, STALE)
    assert isinstance(r, Reading)
    for f in ("volume_cuft", "flow_per_hour", "flow_recent_per_hour", "seconds_since_packet",
              "snr", "rssi", "freq", "packets_per_hour", "rtl433_restarts", "packets_total",
              "stale", "error", "updated"):
        assert hasattr(r, f), f


def test_never_received_is_stale():
    r = MeterTracker(METER_ID).reading(T0, STALE)
    assert r.stale is True
    assert r.volume_cuft is None
    assert r.seconds_since_packet is None
    assert r.packets_total == 0


def test_filters_wrong_id_and_model():
    t = MeterTracker(METER_ID)
    assert t.ingest(pkt(5, id=1234567), T0) is False
    assert t.ingest(pkt(41774, model="Neptune-R900"), T0) is False
    assert t.ingest({"garbage": True}, T0) is False
    assert t.reading(T0, STALE).volume_cuft is None
    assert t.ingest(pkt(41774), T0) is True
    r = t.reading(T0 + 1, STALE)
    assert r.volume_cuft == 41774
    assert r.packets_total == 1


def test_id_as_string_is_not_confused_with_neighbour():
    t = MeterTracker(METER_ID)
    assert t.ingest(pkt(5, id="1234567"), T0) is False


def test_volume_is_cubic_feet_one_to_one():
    """volume_gal from rtl_433 is CUBIC FEET for this meter: no 7.48 conversion."""
    t = MeterTracker(METER_ID)
    t.ingest(pkt(41774), T0)
    r = t.reading(T0, STALE)
    assert r.volume_cuft == pytest.approx(41774.0)
    assert r.volume_cuft != pytest.approx(41774 / 7.480519)
    assert r.volume_cuft != pytest.approx(41774 * 7.480519)


def test_signal_fields_captured():
    t = MeterTracker(METER_ID)
    t.ingest(pkt(41774, snr=16.706, rssi=-8.626, freq1=915.993), T0)
    r = t.reading(T0, STALE)
    assert r.snr == pytest.approx(16.706)
    assert r.rssi == pytest.approx(-8.626)
    assert r.freq == pytest.approx(915.993)


def test_long_flat_volume_is_not_stale_while_packets_arrive():
    t = MeterTracker(METER_ID)
    now = T0
    for _ in range(12 * 30):  # 12 hours, a packet every 2 minutes, never changing
        assert t.ingest(pkt(41774), now)
        now += 120
    r = t.reading(now, STALE)
    assert r.stale is False
    assert r.error is None
    assert r.seconds_since_packet == pytest.approx(120)
    assert r.flow_per_hour == pytest.approx(0.0)
    assert r.flow_recent_per_hour == pytest.approx(0.0)


def test_stale_after_threshold_with_no_packets():
    t = MeterTracker(METER_ID)
    t.ingest(pkt(41774), T0)
    assert t.reading(T0 + STALE - 1, STALE).stale is False
    r = t.reading(T0 + STALE + 1, STALE)
    assert r.stale is True
    assert r.seconds_since_packet == pytest.approx(STALE + 1)
    assert r.volume_cuft == 41774  # last good value kept
    # and it recovers when a packet arrives
    t.ingest(pkt(41774), T0 + 2000)
    assert t.reading(T0 + 2001, STALE).stale is False


def test_age_grows_between_packets():
    t = MeterTracker(METER_ID)
    t.ingest(pkt(41774), T0)
    assert t.reading(T0 + 30, STALE).seconds_since_packet == pytest.approx(30)
    assert t.reading(T0 + 400, STALE).seconds_since_packet == pytest.approx(400)


def test_decreasing_register_dropped():
    t = MeterTracker(METER_ID)
    t.ingest(pkt(41774), T0)
    assert t.ingest(pkt(71000), T0 + 60) is False
    assert t.ingest(pkt(0), T0 + 120) is False
    r = t.reading(T0 + 130, STALE)
    assert r.volume_cuft == 41774
    assert r.packets_total == 1


def test_flow_windows_known_numbers():
    """Register 100 -> 103 at +4h -> 104 at +5.5h -> 106 at +6h, flat packets in between.

    6 h window ending at the last packet starts at T0 where the register read 100:
        (106 - 100) / 6 h = 1.0 cu ft/h
    1 h window starts at +5h, when the register read 103 (set at +4h):
        (106 - 103) / 1 h = 3.0 cu ft/h
    """
    t = MeterTracker(METER_ID)
    schedule = {0: 100, 4 * 3600: 103, 5.5 * 3600: 104, 6 * 3600: 106}
    vol = 100
    s = 0.0
    while s <= 6 * 3600:
        for k, v in schedule.items():
            if s >= k:
                vol = max(vol, v)
        assert t.ingest(pkt(vol), T0 + s), s
        s += 150  # a packet every 2.5 min
    # ensure the final packet lands exactly at +6h with 106
    last = T0 + 6 * 3600
    assert (s - 150) == 6 * 3600
    r = t.reading(last + 10, STALE)
    assert r.volume_cuft == 106
    assert r.flow_per_hour == pytest.approx(1.0, rel=0.02)
    assert r.flow_recent_per_hour == pytest.approx(3.0, rel=0.02)


def test_packets_per_hour_rolling():
    t = MeterTracker(METER_ID)
    now = T0
    for _ in range(60):  # 3 hours at one packet per 3 min = 20/h
        t.ingest(pkt(41774), now)
        now += 180
    r = t.reading(now - 180 + 1, STALE)
    assert r.packets_per_hour == pytest.approx(20, abs=1.5)
    assert r.packets_total == 60


def test_fixture_replay():
    t = MeterTracker(METER_ID)
    now = T0
    packets = load_packets()
    for p in packets:
        assert t.ingest(p, now)
        now += 120
    r = t.reading(now, STALE)
    assert r.volume_cuft == packets[-1]["volume_gal"] == 41777
    assert r.packets_total == len(packets)


# ---- persistence -------------------------------------------------------------------

COLLECTOR_KEYS = {"updated", "meter_id", "volume", "volume_units", "flow_per_hour",
             "flow_recent_per_hour", "seconds_since_packet", "snr", "rssi", "freq",
             "packets_total", "packets_per_hour", "rtl433_restarts", "history"}


def _tracker_with_data():
    t = MeterTracker(METER_ID)
    for i, v in enumerate([41770, 41770, 41771, 41772, 41772, 41774]):
        t.ingest(pkt(v, snr=16.706, rssi=-8.626, freq1=915.993), T0 + i * 600)
    return t


def test_to_json_matches_collector_schema():
    t = _tracker_with_data()
    d = t.to_json(T0 + 3221) if _accepts_now() else t.to_json()
    collector = json.loads((FIXTURES / "state.json").read_text())
    missing = (COLLECTOR_KEYS & set(collector)) - set(d)
    assert not missing, f"to_json missing collector keys: {missing}"
    assert d["meter_id"] == METER_ID
    assert d["volume"] == 41774
    assert d["volume_units"] == "cubic_feet"
    assert isinstance(d["history"], list) and all(len(p) == 2 for p in d["history"])
    json.dumps(d)  # must be JSON-serialisable


def _accepts_now():
    import inspect
    return "now" in inspect.signature(MeterTracker.to_json).parameters


def test_round_trip():
    t = _tracker_with_data()
    now = T0 + 3221
    d = t.to_json(now) if _accepts_now() else t.to_json()
    t2 = MeterTracker.from_json(json.loads(json.dumps(d)))
    r1, r2 = t.reading(now, STALE), t2.reading(now, STALE)
    assert r2.volume_cuft == r1.volume_cuft == 41774
    assert r2.snr == pytest.approx(r1.snr)
    assert r2.rssi == pytest.approx(r1.rssi)
    assert r2.freq == pytest.approx(r1.freq)
    assert r2.packets_total == r1.packets_total
    if _accepts_now():
        assert r2.seconds_since_packet == pytest.approx(r1.seconds_since_packet, abs=1)
        assert r2.flow_per_hour == pytest.approx(r1.flow_per_hour, rel=0.01)
    # history survives
    assert t2.to_json(now)["history"] if _accepts_now() else t2.to_json()["history"]


def test_from_collector_state_json():
    d = json.loads((FIXTURES / "state.json").read_text())
    t = MeterTracker.from_json(d)
    from datetime import datetime
    updated = datetime.fromisoformat(d["updated"]).timestamp()
    r = t.reading(updated + 100, STALE)
    assert r.volume_cuft == 41774.0
    assert r.packets_total == 12114
    assert r.rtl433_restarts == 0
    # seconds_since_packet (221 as of `updated`) + 100 s since
    assert r.seconds_since_packet == pytest.approx(321, abs=1)
    assert r.stale is False
    # a restored tracker that hears nothing goes stale on schedule
    assert t.reading(updated + 900, STALE).stale is True


# ---- forward-jump rejection / re-baseline (review finding 1) ------------------------

def test_reading_has_recovery_fields_with_defaults():
    r = Reading.empty("x")
    assert (r.usb_resets, r.power_cycles, r.reboots_requested, r.recovery_step) == (0, 0, 0, "none")


def test_forward_jump_rejected_feed_not_frozen():
    t = MeterTracker(METER_ID)
    assert t.ingest(pkt(41774), T0)
    # bad decode: +928k cu ft in a minute
    assert t.ingest(pkt(999999), T0 + 60) is False
    assert t.last_result == "rejected"
    assert t.last_heard_mono == T0 + 60  # radio alive all the same
    # the real register keeps flowing
    assert t.ingest(pkt(41775), T0 + 120)
    r = t.reading(T0 + 130, STALE)
    assert r.volume_cuft == 41775 and r.packets_total == 2


def test_plausible_big_usage_accepted():
    t = MeterTracker(METER_ID)
    t.ingest(pkt(1000), T0)
    # one hour at the physical max (~1300 cu ft/h) is still accepted
    assert t.ingest(pkt(1000 + 1300), T0 + 3600)
    assert t.ingest(pkt(1000 + 1300 + 1311), T0 + 7200) is False  # > max*1h + 10


def test_rebaseline_after_bad_high_value():
    """A bad decode became the baseline (first packet): 3 agreeing lower packets over
    >= 10 min re-baseline instead of freezing the feed forever."""
    t = MeterTracker(METER_ID)
    assert t.ingest(pkt(999999), T0)
    assert t.ingest(pkt(41774), T0 + 120) is False
    assert t.ingest(pkt(41774), T0 + 300) is False
    assert t.ingest(pkt(41775), T0 + 600) is False  # 3 agree but span 480 s < 600 s
    assert t.ingest(pkt(41775), T0 + 720) is True  # span 600 s
    r = t.reading(T0 + 730, STALE)
    assert r.volume_cuft == 41775
    assert r.flow_per_hour is None  # history restarted: unknown, never huge/negative
    assert t.ingest(pkt(41776), T0 + 900)
    assert t.reading(T0 + 901, STALE).flow_per_hour == pytest.approx(20.0)  # 1 cu ft / 180 s


def test_rebaseline_needs_consistent_candidates():
    t = MeterTracker(METER_ID)
    t.ingest(pkt(999999), T0)
    for i, v in enumerate([41774, 5000, 41774, 5000, 41774]):
        assert t.ingest(pkt(v), T0 + 300 * (i + 1)) is False
    assert t.reading(T0 + 2000, STALE).volume_cuft == 999999


def test_corrupt_values_in_saved_state_tolerated():
    d = json.loads((FIXTURES / "state.json").read_text())
    d.update(packets_total=1e400, rtl433_restarts="lots", snr=None, volume=41774,
             history=[[1, 2], "junk", [None, 5], [1e400, 3], 7, {"a": 1}], usb_resets=-3,
             recovery_step="rm -rf")
    t = MeterTracker.from_json(d)
    r = t.reading(t.last_packet + 10, STALE)
    assert r.packets_total == 0 and r.rtl433_restarts == 0 and r.usb_resets == 0
    assert r.recovery_step == "none"
    assert r.volume_cuft == 41774


@pytest.mark.parametrize("doc", [None, [], 1e400, "x"])
def test_from_json_rejects_non_objects(doc):
    with pytest.raises((ValueError, TypeError)):
        MeterTracker.from_json(doc)
