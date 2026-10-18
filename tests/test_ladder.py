"""Recovery ladder in Rtl433Source, driven tick by tick with a synthetic monotonic clock.

No real hardware, no real helper: _helper_request is faked, rtl_433 is a FakeProc
(except in the one end-to-end test that uses tests/fake_rtl_433.py).
"""

import asyncio
import dataclasses
import json
import time
from types import SimpleNamespace

import pytest

from conftest import METER_ID
from water_bacnet import sources
from water_bacnet.config import load_config
from water_bacnet.meter import MeterTracker
from water_bacnet.sources import Rtl433Source

SILENT = 1800.0
DAY = 86400.0


def _ns(obj, **over):
    d = {}
    if obj is not None:
        if dataclasses.is_dataclass(obj):
            d = {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
        else:
            d = dict(vars(obj))
    d.update(over)
    return SimpleNamespace(**d)


def make_cfg(tmp_path, recovery=None, storage=None, source=None, meter=None):
    """A config object with [recovery]/new [storage] keys, whether or not config.py has
    them yet (sources reads them through cfg_get with defaults)."""
    base = load_config(None)
    src = dataclasses.replace(base.source, **({"restart_if_silent_s": SILENT} | (source or {})))
    st = _ns(base.storage, state_dir=str(tmp_path / "state"), **(storage or {}))
    rec = _ns(getattr(base, "recovery", None), **(recovery or {}))
    m = dataclasses.replace(base.meter, **(meter or {}))
    return SimpleNamespace(meter=m, source=src, storage=st, bacnet=base.bacnet, recovery=rec)


class FakeProc:
    def __init__(self, killable=True):
        self.returncode = None
        self.killed = 0
        self.killable = killable
        self.pid = 4242

    def kill(self):
        self.killed += 1
        if self.killable:
            self.returncode = -9


def pkt(volume=41774, id=METER_ID):
    return json.dumps({"model": "Badger-ORION", "id": id, "volume_gal": volume,
                       "snr": 16.0, "rssi": -9.0, "freq1": 916.4})


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(sources, "UNKILLABLE_S", 0.15)
    monkeypatch.setattr(sources, "HELPER_POLL_S", 0.02)


class Rig:
    """Source + fake helper + synthetic clock."""

    def __init__(self, tmp_path, monkeypatch, ok=True, **cfg_kw):
        self.cfg = make_cfg(tmp_path, **cfg_kw)
        self.src = Rtl433Source(self.cfg, MeterTracker(METER_ID))
        self.m0 = self.src._ladder_ref
        self.w0 = 1_790_000_000.0
        self.calls = []
        self.ok = ok
        self.spawn_held = []

        async def fake_helper(action):
            self.calls.append(action)
            self.spawn_held.append(not self.src._may_spawn.is_set())
            return (self.ok, "fake")
        monkeypatch.setattr(self.src, "_helper_request", fake_helper)
        self.src.proc = FakeProc()

    async def tick(self, t):
        """Watchdog iteration at monotonic m0 + t; waits for any step it started."""
        src = self.src
        if src.proc is None or src.proc.returncode is not None:
            src.proc = FakeProc()  # the run loop would have respawned rtl_433
        await src._watchdog_once(now=self.w0 + t, mono=self.m0 + t)
        if src._step_task is not None:
            await src._step_task
        return src.tracker.recovery_step

    def heard(self, t, volume=41774):
        return self.src.handle_line(pkt(volume), self.w0 + t, self.m0 + t)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 10))


def test_ladder_order_default_config(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)

    async def go():
        steps = [await rig.tick(SILENT * k) for k in range(1, 6)]
        return steps
    steps = run(go())
    # reboot is off by default
    assert steps == ["restart", "usb_reset", "power_cycle", "exhausted", "exhausted"]
    assert rig.calls == ["usb_reset", "power_cycle"]
    r = rig.src.latest(rig.w0 + SILENT * 5, rig.m0 + SILENT * 5)
    assert (r.usb_resets, r.power_cycles, r.reboots_requested) == (1, 1, 0)
    assert r.recovery_step == "exhausted"
    assert r.stale and "recovery: exhausted" in r.error
    # rtl_433 kept off the dongle while the privileged step ran
    assert rig.spawn_held == [True, True]
    assert rig.src._may_spawn.is_set()


def test_one_step_per_silent_period(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)

    async def go():
        out = []
        for t in (60, SILENT - 1, SILENT, SILENT + 15, SILENT * 1.5, SILENT * 2 - 1, SILENT * 2):
            out.append(await rig.tick(t))
        return out
    out = run(go())
    assert out == ["none", "none", "restart", "restart", "restart", "restart", "usb_reset"]
    assert rig.calls == ["usb_reset"]


def test_disabled_steps_are_skipped(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, recovery={"usb_reset": False, "reboot": True})

    async def go():
        return [await rig.tick(SILENT * k) for k in range(1, 5)]
    assert run(go()) == ["restart", "power_cycle", "reboot", "exhausted"]
    assert rig.calls == ["power_cycle", "reboot"]
    r = rig.src.latest(rig.w0, rig.m0 + SILENT * 4)
    assert (r.usb_resets, r.power_cycles, r.reboots_requested) == (0, 1, 1)


def test_only_restart_enabled(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, recovery={"usb_reset": False, "power_cycle": False})

    async def go():
        return [await rig.tick(SILENT * k) for k in range(1, 4)]
    assert run(go()) == ["restart", "exhausted", "exhausted"]
    assert rig.calls == []


def test_failed_step_counted_and_ladder_continues(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, ok=False)

    async def go():
        return [await rig.tick(SILENT * k) for k in range(1, 5)]
    assert run(go()) == ["restart", "usb_reset", "power_cycle", "exhausted"]
    t = rig.src.tracker
    assert (t.usb_resets, t.power_cycles) == (1, 1)


def test_packet_resets_ladder(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)

    async def go():
        assert await rig.tick(SILENT) == "restart"
        assert await rig.tick(SILENT * 2) == "usb_reset"
        assert rig.heard(SILENT * 2 + 100)
        assert rig.src.tracker.recovery_step == "none"
        # silence is now measured from the packet: nothing until a full period later
        assert await rig.tick(SILENT * 3) == "none"
        assert await rig.tick(SILENT * 3 + 100) == "restart"
    run(go())
    assert rig.calls == ["usb_reset"]
    assert rig.src.tracker.usb_resets == 1  # counters are lifetime attempts, not reset


def test_rejected_but_valid_packet_counts_as_radio_alive(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)

    async def go():
        assert rig.heard(10, 41774)
        assert await rig.tick(10 + SILENT) == "restart"
        # a bad decode (absurd forward jump) from our id: dropped, but the radio works
        assert rig.heard(10 + SILENT + 60, 9_999_999) is False
        assert rig.src.tracker.last_result == "rejected"
        assert rig.src.tracker.recovery_step == "none"
        assert await rig.tick(10 + SILENT * 2) == "none"
    run(go())


def test_exhausted_backoff_doubles_and_caps(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    src = rig.src

    async def go():
        t = 0.0
        for _ in range(4):
            t += SILENT
            await rig.tick(t)
        assert src.tracker.recovery_step == "exhausted"
        retry_at = src._next_retry_mono - rig.m0
        assert retry_at - t == pytest.approx(DAY)
        n_calls = len(rig.calls)

        # During the backoff: rtl_433 restarts only, no privileged step.
        for k in range(1, 6):
            proc = FakeProc()
            src.proc = proc
            assert await rig.tick(t + SILENT * k) == "exhausted"
            assert proc.killed == 1
        assert len(rig.calls) == n_calls

        delays = []
        for _ in range(6):
            t = src._next_retry_mono - rig.m0
            assert await rig.tick(t) == "usb_reset"
            t += SILENT
            assert await rig.tick(t) == "power_cycle"
            t += SILENT
            assert await rig.tick(t) == "exhausted"
            delays.append(src._next_retry_mono - rig.m0 - t)
        return delays
    delays = run(go())
    assert [d / DAY for d in delays] == pytest.approx([2, 4, 7, 7, 7, 7])
    assert rig.calls.count("usb_reset") == 7


def test_unkillable_child_jumps_to_usb_reset(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    stuck = FakeProc(killable=False)
    rig.src.proc = stuck

    async def go():
        return await rig.tick(SILENT)
    assert run(go()) == "usb_reset"
    assert stuck.killed == 1
    assert rig.calls == ["usb_reset"]
    assert rig.src.tracker.usb_resets == 1
    assert rig.src._unkillable_pending is False
    assert stuck in rig.src._abandoned


def test_unkillable_after_eof_jumps_on_next_tick(tmp_path, monkeypatch):
    """The run loop flags an unkillable child; the next watchdog tick acts on it at once."""
    rig = Rig(tmp_path, monkeypatch)

    async def go():
        rig.src._unkillable_pending = True
        return await rig.tick(60)  # far less than a silent period
    assert run(go()) == "usb_reset"
    assert rig.calls == ["usb_reset"]


def test_ladder_state_persists(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)

    async def go():
        for k in range(1, 5):
            await rig.tick(SILENT * k)
    run(go())
    rig.src.save_state()
    d = json.loads((tmp_path / "state" / "state.json").read_text())
    assert (d["usb_resets"], d["power_cycles"], d["recovery_step"]) == (1, 1, "exhausted")
    assert d["recovery_next_retry"] > time.time() + DAY - 3600

    src2 = Rtl433Source(rig.cfg, MeterTracker(METER_ID))
    r = src2.latest()
    assert (r.usb_resets, r.power_cycles, r.recovery_step) == (1, 1, "exhausted")
    # the backoff survives the restart (no immediate privileged retry)
    assert src2._next_retry_mono - time.monotonic() > DAY - 3600


def test_saved_state_written_at_each_step(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, storage={"save_interval_s": 10 ** 9})
    sp = tmp_path / "state" / "state.json"

    async def go():
        await rig.tick(SILENT)
        assert json.loads(sp.read_text())["recovery_step"] == "restart"
        await rig.tick(SILENT * 2)
        assert json.loads(sp.read_text())["usb_resets"] == 1
    run(go())


# ---- the helper client against the real helper (in a thread) ----------------------

def test_helper_round_trip(tmp_path, monkeypatch):
    from water_bacnet.helper import Helper

    cfg = make_cfg(tmp_path)
    src = Rtl433Source(cfg, MeterTracker(METER_ID))
    conf = tmp_path / "config.toml"
    conf.write_text('[source]\ndevice = ":00000001"\n')
    done = []
    ops = SimpleNamespace(
        find_dongle=lambda vp, serial=None: (1, 4),
        usb_reset=lambda bus, dev: done.append((bus, dev)) or "reset ok",
        power_cycle=None, request_reboot=None)
    helper = Helper(str(conf), str(src.helper_dir), str(tmp_path / "ledger.json"), ops=ops)

    async def fake_path_unit():
        req = src.helper_dir / "request.json"
        while not req.exists():
            await asyncio.sleep(0.01)
        await asyncio.to_thread(helper.run)

    async def go():
        unit = asyncio.create_task(fake_path_unit())
        ok, detail = await src._helper_request("usb_reset")
        await unit
        return ok, detail
    ok, detail = run(go())
    assert ok and detail == "reset ok"
    assert done == [(1, 4)]
    assert not (src.helper_dir / "request.json").exists()
    assert not (src.helper_dir / "response.json").exists()


def test_helper_timeout_is_a_failed_step_and_withdraws_request(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, recovery={"step_timeout_s": 0.2})
    src = Rtl433Source(cfg, MeterTracker(METER_ID))
    ok, detail = run(src._helper_request("power_cycle"))
    assert ok is False and "no answer" in detail
    assert not (src.helper_dir / "request.json").exists()


def test_stale_response_with_other_id_is_ignored(tmp_path):
    cfg = make_cfg(tmp_path, recovery={"step_timeout_s": 0.2})
    src = Rtl433Source(cfg, MeterTracker(METER_ID))

    async def go():
        task = asyncio.create_task(src._helper_request("usb_reset"))
        await asyncio.sleep(0.05)
        (src.helper_dir / "response.json").write_text(
            json.dumps({"id": "someone-else", "ok": True, "detail": "x", "ts": 0}))
        return await task
    ok, _ = run(go())
    assert ok is False


# ---- end to end with the fake rtl_433 ----------------------------------------------

def test_end_to_end_silent_dongle_escalates(tmp_path, fake_rtl, monkeypatch):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_RTL_FIXTURE", str(empty))
    monkeypatch.setenv("FAKE_RTL_ARGV_LOG", str(log))
    monkeypatch.setattr(sources, "WATCHDOG_S", 0.05)
    monkeypatch.setattr(sources, "BACKOFF_MIN_S", 0.1)
    monkeypatch.setattr(sources, "RESTART_DELAY_S", 0.05)
    cfg = make_cfg(tmp_path, source={"rtl433_path": str(fake_rtl), "restart_if_silent_s": 0.4},
                   recovery={"step_timeout_s": 0.2})
    src = Rtl433Source(cfg, MeterTracker(METER_ID))
    beats = []

    def spawns():
        if not log.exists():
            return 0
        return len([r for r in map(json.loads, log.read_text().splitlines()) if "-V" not in r["argv"]])

    async def go():
        task = asyncio.create_task(src.run())
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline and (src.tracker.power_cycles < 1 or spawns() < 3):
            await asyncio.sleep(0.05)
            beats.append(src.heartbeat())
        task.cancel()
        try:
            await asyncio.wait_for(task, 5)
        except asyncio.CancelledError:
            pass
    run(go())
    t = src.tracker
    assert t.usb_resets >= 1 and t.power_cycles >= 1, (t.recovery_step, t.usb_resets)
    assert spawns() >= 3, "rtl_433 not restarted after each step"
    # the watchdog loop kept beating while helper requests were outstanding
    assert beats[-1] > beats[0]
    d = json.loads((tmp_path / "state" / "state.json").read_text())
    assert d["usb_resets"] >= 1
