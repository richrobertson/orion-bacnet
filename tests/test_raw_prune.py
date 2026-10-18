"""raw/*.jsonl retention (SD card must not fill over years) and raw-log filtering."""

import asyncio
import json
from datetime import datetime, timezone

from conftest import METER_ID
from test_ladder import make_cfg
from water_bacnet.meter import MeterTracker
from water_bacnet.sources import Rtl433Source, prune_raw

NOW = datetime(2027, 10, 18, 12, 0, tzinfo=timezone.utc).timestamp()


def _make(raw, names):
    raw.mkdir(parents=True, exist_ok=True)
    for n in names:
        (raw / n).write_text("{}\n")


def test_prune_older_than_retention(tmp_path):
    raw = tmp_path / "raw"
    _make(raw, ["2026-10-17.jsonl", "2026-10-18.jsonl", "2026-10-19.jsonl",
                "2027-10-18.jsonl", "notes.txt", "2020-01-01.jsonl.gz", "2026-13-40.jsonl"])
    removed = prune_raw(raw, 365, NOW)
    assert removed == ["2026-10-17.jsonl"]
    left = sorted(p.name for p in raw.iterdir())
    assert left == ["2020-01-01.jsonl.gz", "2026-10-18.jsonl", "2026-10-19.jsonl",
                    "2026-13-40.jsonl", "2027-10-18.jsonl", "notes.txt"]


def test_zero_retention_keeps_everything(tmp_path):
    raw = tmp_path / "raw"
    _make(raw, ["2000-01-01.jsonl"])
    assert prune_raw(raw, 0, NOW) == []
    assert (raw / "2000-01-01.jsonl").exists()


def test_missing_dir_is_fine(tmp_path):
    assert prune_raw(tmp_path / "nope", 365, NOW) == []


def test_forward_clock_jump_does_not_wipe_archive(tmp_path):
    """Wall clock wrongly 2 years ahead: retention counts from the newest file."""
    raw = tmp_path / "raw"
    _make(raw, ["2027-01-01.jsonl", "2027-10-18.jsonl"])
    later = datetime(2029, 10, 18, tzinfo=timezone.utc).timestamp()
    assert prune_raw(raw, 365, later) == []


def test_source_prunes_on_watchdog_and_logs_only_our_meter(tmp_path):
    cfg = make_cfg(tmp_path, storage={"raw_retention_days": 30})
    src = Rtl433Source(cfg, MeterTracker(METER_ID))
    _make(src.raw_dir, ["2020-01-01.jsonl"])
    now = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc).timestamp()
    mono = 100.0
    lines = [
        {"model": "Badger-ORION", "id": METER_ID, "volume_gal": 41774},  # accepted
        {"model": "Badger-ORION", "id": 1234567, "volume_gal": 5},  # neighbour
        {"model": "Neptune-R900", "id": METER_ID, "consumption": 9},  # other model
        {"model": "Badger-ORION", "id": METER_ID, "volume_gal": 9_999_999},  # bad decode
        {"model": "Badger-ORION", "id": METER_ID, "volume_gal": "x"},  # invalid volume
    ]

    async def go():
        for i, p in enumerate(lines):
            src.handle_line(json.dumps(p), now + i, mono + i)
        await src._flush_io(mono + 10)
        await src._watchdog_once(now + 20, mono + 20)
    asyncio.run(go())
    assert not (src.raw_dir / "2020-01-01.jsonl").exists()
    rows = [json.loads(ln) for ln in (src.raw_dir / "2026-10-18.jsonl").read_text().splitlines()]
    assert [(r["id"], r["volume_gal"]) for r in rows] == [(METER_ID, 41774), (METER_ID, 9_999_999)]
    assert all("_rx" in r for r in rows)


def test_raw_log_off(tmp_path):
    cfg = make_cfg(tmp_path, storage={"raw_log": False})
    src = Rtl433Source(cfg, MeterTracker(METER_ID))
    src.handle_line(json.dumps({"model": "Badger-ORION", "id": METER_ID, "volume_gal": 1}), 1e9, 1.0)
    asyncio.run(src._flush_io(2.0))
    assert not src.raw_dir.exists() or not list(src.raw_dir.iterdir())
