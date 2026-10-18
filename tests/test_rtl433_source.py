"""Rtl433Source driven by tests/fake_rtl_433.py instead of a real SDR.

Every test is wrapped in asyncio.wait_for so nothing can hang the suite.
"""

import asyncio
import json
import time

import pytest

from conftest import METER_ID, load_packets, pid_alive, write_config
from water_bacnet import sources
from water_bacnet.meter import MeterTracker
from water_bacnet.sources import Rtl433Source


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    # Implementation constants (not part of the contract); shorten if present so the
    # restart test stays well under 10 s. raising=False keeps this harmless if renamed.
    monkeypatch.setattr(sources, "BACKOFF_MIN_S", 0.2, raising=False)
    monkeypatch.setattr(sources, "WATCHDOG_S", 0.2, raising=False)


def _cfg(tmp_path, fake_rtl, extra_source=""):
    state_dir = tmp_path / "state"
    return write_config(tmp_path / "config.toml", f"""
[meter]
id = {METER_ID}
stale_after_s = 900
[source]
mode = "rtl_433"
rtl433_path = "{fake_rtl}"
device = ":00000001"
frequency = "916.45M"
sample_rate = "1600k"
protocol = 223
{extra_source}
[storage]
state_dir = "{state_dir}"
raw_log = true
[bacnet]
refresh_s = 1
"""), state_dir


async def _run_until(src, cond, timeout):
    task = asyncio.create_task(src.run())
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if task.done():
                task.result()  # surface a crash in run()
                raise AssertionError("run() returned; it should run until cancelled")
            if cond():
                return True
            await asyncio.sleep(0.05)
        return False
    finally:
        task.cancel()
        try:
            await asyncio.wait_for(task, 5)
        except asyncio.CancelledError:
            pass


def _invocations(log):
    if not log.exists():
        return []
    rows = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
    return [r for r in rows if "-V" not in r["argv"]]


def test_accepts_only_our_meter_and_persists(tmp_path, fake_rtl, monkeypatch):
    log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_RTL_DELAY", "0.01")
    monkeypatch.setenv("FAKE_RTL_ARGV_LOG", str(log))
    cfg, state_dir = _cfg(tmp_path, fake_rtl)
    tracker = MeterTracker(METER_ID)
    src = Rtl433Source(cfg, tracker)
    n = len(load_packets())

    ok = asyncio.run(asyncio.wait_for(
        _run_until(src, lambda: src.latest(time.time()).packets_total >= n, 6), 8))
    assert ok, f"only {src.latest(time.time()).packets_total}/{n} packets accepted"

    r = src.latest(time.time())
    assert r.packets_total == n  # neighbour 1234567 and the Neptune with our id ignored
    assert r.volume_cuft == 41777  # cubic feet, 1:1 with volume_gal
    assert r.stale is False
    assert r.seconds_since_packet is not None and r.seconds_since_packet < 5

    # command line
    inv = _invocations(log)
    assert inv, "fake rtl_433 never started"
    argv = inv[0]["argv"]
    joined = " ".join(argv)
    for frag in ("-d :00000001", "-f 916.45M", "-s 1600k", "-R 223", "-F json"):
        assert frag in joined, (frag, argv)
    assert "level" in argv and "time:iso" in argv

    # persisted atomically into state_dir, collector state.json schema
    sp = state_dir / "state.json"
    assert sp.exists(), list(state_dir.rglob("*"))
    d = json.loads(sp.read_text())
    assert d["meter_id"] == METER_ID
    assert d["volume"] == 41777
    assert d["packets_total"] == n
    assert not list(state_dir.glob("*.tmp")), "temp file left behind"


def test_state_survives_restart_of_service(tmp_path, fake_rtl, monkeypatch):
    monkeypatch.setenv("FAKE_RTL_DELAY", "0.01")
    cfg, state_dir = _cfg(tmp_path, fake_rtl)
    src = Rtl433Source(cfg, MeterTracker(METER_ID))
    n = len(load_packets())
    asyncio.run(asyncio.wait_for(
        _run_until(src, lambda: src.latest(time.time()).packets_total >= n, 6), 8))

    # A new source on the same state_dir, with a fake that prints nothing, starts with
    # the persisted values rather than unknown.
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    monkeypatch.setenv("FAKE_RTL_FIXTURE", str(empty))
    src2 = Rtl433Source(cfg, MeterTracker(METER_ID))

    async def go():
        await _run_until(src2, lambda: False, 0.3)
    asyncio.run(asyncio.wait_for(go(), 8))
    r = src2.latest(time.time())
    assert r.volume_cuft == 41777
    assert r.packets_total >= n


def test_restart_counted_when_rtl433_exits(tmp_path, fake_rtl, monkeypatch):
    log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_RTL_DELAY", "0.01")
    monkeypatch.setenv("FAKE_RTL_EXIT_AFTER", "2")
    monkeypatch.setenv("FAKE_RTL_ARGV_LOG", str(log))
    cfg, _ = _cfg(tmp_path, fake_rtl)
    src = Rtl433Source(cfg, MeterTracker(METER_ID))

    t0 = time.monotonic()
    ok = asyncio.run(asyncio.wait_for(
        _run_until(src, lambda: src.latest(time.time()).rtl433_restarts >= 2, 8), 9))
    elapsed = time.monotonic() - t0
    r = src.latest(time.time())
    assert ok, f"rtl433_restarts={r.rtl433_restarts} after {elapsed:.1f}s"
    assert len(_invocations(log)) >= 2, "rtl_433 was not respawned"
    assert r.packets_total >= 4  # 2 packets per run, at least 2 runs
    assert elapsed < 9


def test_no_output_does_not_hang_and_cancel_kills_child(tmp_path, fake_rtl, monkeypatch):
    log = tmp_path / "argv.jsonl"
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    monkeypatch.setenv("FAKE_RTL_FIXTURE", str(empty))
    monkeypatch.setenv("FAKE_RTL_ARGV_LOG", str(log))
    cfg, _ = _cfg(tmp_path, fake_rtl)
    src = Rtl433Source(cfg, MeterTracker(METER_ID))

    async def go():
        task = asyncio.create_task(src.run())
        # While rtl_433 is silent the event loop must stay responsive and latest()
        # must answer immediately (the old collector froze here).
        ticks = 0
        for _ in range(10):
            await asyncio.sleep(0.1)
            t = time.monotonic()
            r = src.latest(time.time())
            assert time.monotonic() - t < 0.1
            assert r.stale is True
            ticks += 1
        assert not task.done(), "run() exited while rtl_433 was merely silent"
        task.cancel()
        try:
            await asyncio.wait_for(task, 5)
        except asyncio.CancelledError:
            pass
        return ticks

    t0 = time.monotonic()
    assert asyncio.run(asyncio.wait_for(go(), 8)) == 10
    assert time.monotonic() - t0 < 8
    inv = _invocations(log)
    assert inv, "fake rtl_433 never started"
    time.sleep(0.2)
    for row in inv:
        assert not pid_alive(row["pid"]), f"rtl_433 pid {row['pid']} left running after cancel"


def test_missing_binary_does_not_crash(tmp_path, monkeypatch):
    cfg, _ = _cfg(tmp_path, tmp_path / "does-not-exist")
    src = Rtl433Source(cfg, MeterTracker(METER_ID))

    async def go():
        await _run_until(src, lambda: False, 0.5)
    asyncio.run(asyncio.wait_for(go(), 8))
    r = src.latest(time.time())
    assert r.stale is True
    assert r.error


# ---- corrupt saved state (review finding 8), heartbeat, supervision ------------------

@pytest.mark.parametrize("body", ["null", "[]", "1e400", '{"meter_id": 7654321, "volume": 7',
                                  '{"meter_id": 1e400}', '{"meter_id": "abc"}',
                                  '{"meter_id": 7654321, "updated": "garbage", '
                                  '"seconds_since_packet": 5}',
                                  '{"meter_id": 7654321, "updated": 12345}',
                                  "\x00\x01\x02"])
def test_corrupt_state_file_moved_aside_and_fresh_start(tmp_path, fake_rtl, body):
    cfg, state_dir = _cfg(tmp_path, fake_rtl)
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(body)
    src = Rtl433Source(cfg, MeterTracker(METER_ID))  # must not raise
    r = src.latest(time.time())
    assert r.stale is True and r.volume_cuft is None and r.packets_total == 0
    assert not (state_dir / "state.json").exists()
    assert len(list(state_dir.glob("state.json.corrupt-*"))) == 1
    # and it saves cleanly over it
    src.save_state()
    assert json.loads((state_dir / "state.json").read_text())["meter_id"] == METER_ID


def test_heartbeat_advances_while_silent(tmp_path, fake_rtl, monkeypatch):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    monkeypatch.setenv("FAKE_RTL_FIXTURE", str(empty))
    monkeypatch.setattr(sources, "WATCHDOG_S", 0.05)
    cfg, _ = _cfg(tmp_path, fake_rtl)
    src = Rtl433Source(cfg, MeterTracker(METER_ID))
    h0 = src.heartbeat()
    assert isinstance(h0, float) and abs(time.monotonic() - h0) < 5

    async def go():
        await _run_until(src, lambda: src.heartbeat() > h0 + 0.1, 3)
    asyncio.run(asyncio.wait_for(go(), 6))
    assert src.heartbeat() > h0


def test_watchdog_survives_exceptions(tmp_path, fake_rtl, monkeypatch):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    monkeypatch.setenv("FAKE_RTL_FIXTURE", str(empty))
    monkeypatch.setattr(sources, "WATCHDOG_S", 0.02)
    cfg, _ = _cfg(tmp_path, fake_rtl)
    src = Rtl433Source(cfg, MeterTracker(METER_ID))
    calls = []
    real = src._watchdog_once

    async def flaky(*a, **kw):
        calls.append(1)
        if len(calls) <= 3:
            raise RuntimeError("boom")
        return await real(*a, **kw)
    monkeypatch.setattr(src, "_watchdog_once", flaky)
    h0 = src.heartbeat()

    async def go():
        await _run_until(src, lambda: len(calls) >= 6 and src.heartbeat() > h0, 3)
    asyncio.run(asyncio.wait_for(go(), 6))
    assert len(calls) >= 6 and src.heartbeat() > h0


def test_state_file_source_overflow_values(tmp_path):
    from water_bacnet.sources import StateFileSource
    sf = tmp_path / "s.json"
    cfg = write_config(tmp_path / "c.toml", f'[meter]\nid = {METER_ID}\n'
                       f'[source]\nmode = "state_file"\nstate_file = "{sf}"\n')
    src = StateFileSource(cfg)
    sf.write_text('{"updated": "2026-10-18T09:58:12+00:00", "meter_id": 7654321, '
                  '"volume": 5, "packets_total": 1e400, "rtl433_restarts": 1e400, '
                  '"seconds_since_packet": 1}')
    r = src.poll()
    assert r.volume_cuft == 5 and r.packets_total == 0 and r.rtl433_restarts == 0
    for body in ("null", "[]", "1e400", '{"meter_id": 1e400, "updated": "2026-10-18T09:58:12", "volume": 1}'):
        sf.write_text(body)
        r = src.poll()  # never raises
        assert r.stale is True
    assert isinstance(src.heartbeat(), float)
