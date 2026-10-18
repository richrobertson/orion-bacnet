"""StateFileSource: follows a standalone collector's /var/lib/water-meter/state.json."""

import asyncio
import dataclasses
import json
import os
from datetime import datetime, timezone

import pytest

from conftest import FIXTURES, write_config
from water_bacnet.sources import StateFileSource

UPDATED = datetime.fromisoformat("2026-10-18T09:58:12+00:00").timestamp()


def _src(tmp_path, refresh=0.05):
    sf = tmp_path / "state.json"
    cfg = write_config(tmp_path / "config.toml", f"""
[meter]
id = 7654321
stale_after_s = 900
[source]
mode = "state_file"
state_file = "{sf}"
[bacnet]
refresh_s = 1
""")
    # Production validation requires refresh_s >= 1; tests poll faster, so override
    # the loaded (validated) config directly.
    cfg = dataclasses.replace(cfg, bacnet=dataclasses.replace(cfg.bacnet, refresh_s=refresh))
    return StateFileSource(cfg), sf


def _write(sf, **over):
    d = json.loads((FIXTURES / "state.json").read_text())
    d.update(over)
    tmp = sf.with_name(sf.name + ".tmp")
    tmp.write_text(json.dumps(d))
    os.replace(tmp, sf)
    return d


def _refresh(src, now):
    """Make the source re-read its file once, then return latest(now).

    Uses poll(now) if the implementation has it; otherwise runs run() briefly
    (refresh_s is 0.05 s in these configs).
    """
    poll = getattr(src, "poll", None)
    if poll is not None:
        poll(now)
        return src.latest(now)

    async def go():
        task = asyncio.create_task(src.run())
        await asyncio.sleep(0.25)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
    return src.latest(now)


def test_fresh_file_values(tmp_path):
    src, sf = _src(tmp_path)
    _write(sf)
    r = _refresh(src, UPDATED + 10)
    assert r.stale is False, r.error
    assert r.error is None
    assert r.volume_cuft == 41774.0  # cubic feet, 1:1
    assert r.snr == pytest.approx(16.706)
    assert r.rssi == pytest.approx(-8.626)
    assert r.freq == pytest.approx(915.993)
    assert r.packets_per_hour == pytest.approx(21.65)
    assert r.packets_total == 12114
    assert r.rtl433_restarts == 0
    assert r.flow_per_hour == pytest.approx(0.0)
    assert r.updated == pytest.approx(UPDATED)


def test_age_is_seconds_since_packet_plus_file_age(tmp_path):
    src, sf = _src(tmp_path)
    _write(sf)
    r = _refresh(src, UPDATED + 10)
    assert r.seconds_since_packet == pytest.approx(221 + 10)
    # no re-read: latest() alone keeps the age growing (frozen collector case)
    assert src.latest(UPDATED + 300).seconds_since_packet == pytest.approx(521)


def test_packet_age_over_threshold_is_stale(tmp_path):
    src, sf = _src(tmp_path)
    _write(sf)
    _refresh(src, UPDATED + 10)
    # file is 700 s old (< 900) but packet age is 221 + 700 = 921 s (> 900)
    r = src.latest(UPDATED + 700)
    assert r.stale is True
    assert r.volume_cuft == 41774.0


def test_old_updated_is_stale_even_with_small_packet_age(tmp_path):
    src, sf = _src(tmp_path)
    _write(sf, seconds_since_packet=0.0)
    r = _refresh(src, UPDATED + 1000)
    assert r.stale is True
    assert r.error


def test_flat_volume_not_stale(tmp_path):
    """Volume identical across many rewrites is normal, not staleness."""
    src, sf = _src(tmp_path)
    for i in range(5):
        upd = UPDATED + i * 15
        _write(sf, updated=datetime.fromtimestamp(upd, timezone.utc).isoformat(),
               seconds_since_packet=30.0)
        r = _refresh(src, upd + 1)
        assert r.stale is False, (i, r.error)
        assert r.volume_cuft == 41774.0


def test_missing_file_never_read_is_stale_with_error(tmp_path):
    src, sf = _src(tmp_path)
    r = _refresh(src, UPDATED)
    assert r.stale is True
    assert r.error
    assert r.volume_cuft is None


def test_missing_file_after_good_keeps_last_values(tmp_path):
    src, sf = _src(tmp_path)
    _write(sf)
    assert _refresh(src, UPDATED + 10).stale is False
    sf.unlink()
    r = _refresh(src, UPDATED + 20)
    assert r.stale is True
    assert r.error and ("missing" in r.error.lower() or "no such" in r.error.lower()
                        or "not found" in r.error.lower() or "unreadable" in r.error.lower())
    assert r.volume_cuft == 41774.0  # last good value kept, but flagged


def test_bad_json_is_stale_with_error_and_keeps_values(tmp_path):
    src, sf = _src(tmp_path)
    _write(sf)
    assert _refresh(src, UPDATED + 10).stale is False
    sf.write_text('{"updated": "2026-10-18T09:58:27+00:00", "volume": 717')  # torn write
    r = _refresh(src, UPDATED + 20)
    assert r.stale is True
    assert r.error
    assert r.volume_cuft == 41774.0


def test_recovers_when_fresh_again(tmp_path):
    src, sf = _src(tmp_path)
    _write(sf)
    _refresh(src, UPDATED + 10)
    sf.write_text("not json")
    assert _refresh(src, UPDATED + 20).stale is True
    new_upd = UPDATED + 30
    _write(sf, updated=datetime.fromtimestamp(new_upd, timezone.utc).isoformat(),
           volume=41775.0, seconds_since_packet=5.0)
    r = _refresh(src, new_upd + 2)
    assert r.stale is False, r.error
    assert r.error is None
    assert r.volume_cuft == 41775.0
    assert r.seconds_since_packet == pytest.approx(7)


def test_run_loop_reads_file(tmp_path):
    """run() itself (not just poll) picks the file up within a few refresh periods."""
    src, sf = _src(tmp_path, refresh=0.05)
    import time
    now = time.time()
    _write(sf, updated=datetime.fromtimestamp(now, timezone.utc).isoformat(),
           seconds_since_packet=12.0)

    async def go():
        task = asyncio.create_task(src.run())
        try:
            for _ in range(40):
                await asyncio.sleep(0.05)
                r = src.latest(time.time())
                if r.volume_cuft is not None:
                    return r
            return src.latest(time.time())
        finally:
            task.cancel()
            try:
                await asyncio.wait_for(task, 2)
            except asyncio.CancelledError:
                pass

    r = asyncio.run(asyncio.wait_for(go(), 5))
    assert r.volume_cuft == 41774.0
    assert r.stale is False
    assert 12 <= r.seconds_since_packet < 15
