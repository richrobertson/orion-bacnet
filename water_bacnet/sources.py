"""Data sources: spawn rtl_433 ourselves, or follow an existing collector's state.json.

Both expose `async run()` (runs until cancelled), `latest(now) -> Reading` and
`heartbeat() -> float` (time.monotonic() of the last completed iteration of the
internal loop; the service only pets the systemd watchdog while it is recent).

Rtl433Source owns the recovery ladder for a wedged RTL-SDR, one step per silent
period (`restart_if_silent_s`, measured on the monotonic clock):

    restart -> usb_reset -> power_cycle -> reboot (only if enabled) -> exhausted

Steps disabled in [recovery] are skipped. Any packet from our meter id (accepted, or
dropped as a bad decode) resets the ladder to "none". Once exhausted, rtl_433 is still
restarted every silent period, and the privileged part of the ladder is retried after
24 h, then 48 h, ... capped at 7 days. An rtl_433 that is not reaped UNKILLABLE_S
after SIGKILL (stuck in the kernel) jumps straight to usb_reset.

usb_reset / power_cycle / reboot need root, so the service only drops a request file
for the root oneshot water_bacnet.helper (see helper.py for the protocol). A timeout or
a refusal is a failed step: logged, still counted as an attempt, and the ladder moves
on at the next silent period.

Disk writes (state.json, raw logs, helper files) run in a worker thread so a slow SD
card never stalls the event loop. state.json is written every save_interval_s (default
900 s), at every ladder step and at shutdown -- deliberately NOT on every register
change: at ~22 packets/h that would be ~500 SD-card writes a day for years, and losing
up to 15 min of state in a crash costs nothing real (the register is absolute and the
next packet, minutes later, restores it; only a little flow history is lost).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .meter import MeterTracker, Reading, _number, parse_iso
from .usbctl import DEFAULTS

log = logging.getLogger(__name__)

BACKOFF_MIN_S = 5.0
BACKOFF_MAX_S = 300.0
HEALTHY_RUN_S = 300.0  # a process that lived this long resets the backoff
WATCHDOG_S = 15.0
UNKILLABLE_S = 30.0  # not reaped this long after SIGKILL = stuck in the kernel
HELPER_POLL_S = 0.5
RESTART_DELAY_S = 2.0  # after a deliberate (ladder) stop of rtl_433
EXHAUSTED_BACKOFF_S = 86400.0
EXHAUSTED_BACKOFF_MAX_S = 7 * 86400.0
PRUNE_EVERY_S = 86400.0
KEEP_CORRUPT = 5

LADDER = ("restart", "usb_reset", "power_cycle", "reboot")
PRIVILEGED = ("usb_reset", "power_cycle", "reboot")


def cfg_get(cfg, section: str, key: str):
    """The one accessor for config keys that config.py may not know yet.

    Falls back to usbctl.DEFAULTS, so this works before and after the [recovery] and
    new [storage] keys exist in config.py.
    """
    default = DEFAULTS[(section, key)]
    sec = getattr(cfg, section, None)
    if sec is None:
        return default
    return getattr(sec, key, default)


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_json(path: Path, data: dict) -> None:
    """tmp + fsync + rename + fsync(dir): the file is either old or new after a power cut."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as fh:
        json.dump(data, fh, separators=(",", ":"))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


_RAW_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})\.jsonl")


def prune_raw(raw_dir, retention_days, now: Optional[float] = None) -> list:
    """Delete raw/YYYY-MM-DD.jsonl older than retention_days. Returns removed names.

    retention_days <= 0 keeps everything. The reference day is min(today, newest file's
    day), so a wall clock that jumps far forward does not wipe the archive before a new
    file has even been written. Other files are never touched.
    """
    if retention_days is None or retention_days <= 0:
        return []
    now = time.time() if now is None else now
    try:
        entries = list(Path(raw_dir).iterdir())
    except OSError:
        return []
    dated = []
    for p in entries:
        m = _RAW_NAME.fullmatch(p.name)
        if not m:
            continue
        try:
            dated.append((date.fromisoformat(m.group(1)), p))
        except ValueError:
            continue
    if not dated:
        return []
    today = datetime.fromtimestamp(now, timezone.utc).date()
    ref = min(today, max(d for d, _ in dated))
    cutoff = ref - timedelta(days=int(retention_days))
    removed = []
    for d, p in sorted(dated):
        if d < cutoff:
            try:
                p.unlink()
                removed.append(p.name)
            except OSError as exc:
                log.error("cannot prune %s: %s", p, exc)
    if removed:
        log.info("pruned %d raw log file(s) older than %d days (%s .. %s)",
                 len(removed), retention_days, removed[0], removed[-1])
    return removed


class Rtl433Source:
    def __init__(self, cfg, tracker: Optional[MeterTracker] = None):
        self.cfg = cfg
        self.tracker = tracker if tracker is not None else MeterTracker(cfg.meter.id)
        state_dir = Path(cfg.storage.state_dir)
        self.state_path = state_dir / "state.json"
        self.raw_dir = state_dir / "raw"
        self.helper_dir = state_dir / "helper"
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.last_error: Optional[str] = None
        mono = time.monotonic()
        self._heartbeat = mono
        self._last_save_mono = mono
        self._last_prune_mono: Optional[float] = None
        self._kill_reason: Optional[str] = None
        self._was_stale: Optional[bool] = None
        self._ladder_ref = mono  # silence is measured from max(last heard, this)
        self._backoff = EXHAUSTED_BACKOFF_S
        self._next_retry_mono: Optional[float] = None
        self._step_task: Optional[asyncio.Task] = None
        self._unkillable_pending = False
        self._may_spawn: Optional[asyncio.Event] = None
        self._save_lock: Optional[asyncio.Lock] = None
        self._read_task: Optional[asyncio.Task] = None
        self._abandoned: list = []
        self._got = False
        self._raw_pending: list = []
        self._load_state()
        self._restore_ladder()

    def _ensure_async(self) -> None:
        if self._may_spawn is None:
            self._may_spawn = asyncio.Event()
            self._may_spawn.set()
        if self._save_lock is None:
            self._save_lock = asyncio.Lock()

    def heartbeat(self) -> float:
        return self._heartbeat

    # ---- command line -----------------------------------------------------------

    def command(self) -> list:
        s = self.cfg.source
        args = [s.rtl433_path]
        if s.device:
            args += ["-d", s.device]
        args += ["-f", s.frequency, "-s", s.sample_rate, "-R", str(s.protocol)]
        args += ["-F", "json", "-M", "level", "-M", "time:iso"]
        args += list(s.extra_args)
        return args

    # ---- persistence ------------------------------------------------------------

    def _load_state(self) -> None:
        t = self.tracker
        if t.packets_total or t.last_packet is not None:
            return  # caller handed us a tracker that already has data
        try:
            text = self.state_path.read_text()
        except FileNotFoundError:
            log.info("no saved state at %s; starting empty", self.state_path)
            return
        except (OSError, ValueError) as exc:
            log.error("cannot read saved state %s: %s; starting empty", self.state_path, exc)
            return
        try:
            d = json.loads(text)
            if not isinstance(d, dict):
                raise ValueError(f"not a JSON object ({type(d).__name__})")
            mid = d.get("meter_id", -1)
            if int(mid) != t.meter_id:
                log.warning(
                    "saved state is for meter %s, configured meter is %s; ignoring it",
                    mid, t.meter_id,
                )
                return
            t.restore(d)
        except Exception as exc:  # anything parseable-but-wrong must not crash-loop
            t.__init__(t.meter_id, t.history_hours)
            dest = self.state_path.with_name(f"state.json.corrupt-{int(time.time())}")
            try:
                os.replace(self.state_path, dest)
                where = f"moved to {dest.name}"
            except OSError as exc2:
                where = f"could not move it aside: {exc2}"
            log.error("saved state %s is corrupt (%s: %s); %s; starting fresh",
                      self.state_path, type(exc).__name__, exc, where)
            self._prune_corrupt()
            return
        log.info(
            "restored state: volume=%s cu ft, packets_total=%d, %d history points",
            t.volume, t.packets_total, len(t.history),
        )

    def _prune_corrupt(self) -> None:
        try:
            old = sorted(self.state_path.parent.glob("state.json.corrupt-*"))
        except OSError:
            return
        for p in old[:-KEEP_CORRUPT]:
            try:
                p.unlink()
            except OSError:
                pass

    def _restore_ladder(self) -> None:
        t = self.tracker
        b = t.recovery_backoff_s
        self._backoff = min(max(b, EXHAUSTED_BACKOFF_S), EXHAUSTED_BACKOFF_MAX_S) if b else EXHAUSTED_BACKOFF_S
        if t.recovery_step == "exhausted":
            remaining = self._backoff
            if t.recovery_next_retry is not None:
                remaining = t.recovery_next_retry - time.time()
            remaining = min(max(remaining, 0.0), EXHAUSTED_BACKOFF_MAX_S)
            self._next_retry_mono = time.monotonic() + remaining
        if t.recovery_step != "none":
            log.warning("resuming the recovery ladder at step %r (from saved state)", t.recovery_step)

    def _sync_ladder(self, now: float, mono: float) -> None:
        t = self.tracker
        t.recovery_backoff_s = self._backoff
        t.recovery_next_retry = (None if self._next_retry_mono is None
                                 else now + (self._next_retry_mono - mono))

    def _state_doc(self, now: Optional[float], mono: Optional[float]) -> tuple:
        now = time.time() if now is None else now
        mono = time.monotonic() if mono is None else mono
        self._sync_ladder(now, mono)
        return self.tracker.to_json(now, mono), mono

    def _write_state(self, doc: dict) -> bool:
        try:
            atomic_write_json(self.state_path, doc)
            return True
        except OSError as exc:
            log.error("cannot save state to %s: %s", self.state_path, exc)
            return False

    def save_state(self, now: Optional[float] = None, mono: Optional[float] = None) -> None:
        """Blocking save (shutdown path)."""
        doc, mono = self._state_doc(now, mono)
        self._write_state(doc)
        self._last_save_mono = mono

    async def _save_async(self, now: Optional[float] = None, mono: Optional[float] = None) -> None:
        self._ensure_async()
        doc, mono = self._state_doc(now, mono)
        async with self._save_lock:
            await asyncio.to_thread(self._write_state, doc)
        self._last_save_mono = mono

    def _save_due(self, mono: float) -> bool:
        return mono - self._last_save_mono >= float(cfg_get(self.cfg, "storage", "save_interval_s"))

    def _write_raw(self, items: list) -> None:
        """Append [(wall, pkt), ...] to raw/YYYY-MM-DD.jsonl (worker thread)."""
        by_day: dict = {}
        for now, pkt in items:
            day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
            by_day.setdefault(day, []).append(
                json.dumps(dict(pkt, _rx=round(now, 3)), separators=(",", ":")) + "\n")
        try:
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            for day, lines in by_day.items():
                with (self.raw_dir / f"{day}.jsonl").open("a") as fh:
                    fh.write("".join(lines))
        except OSError as exc:
            log.error("cannot write raw log: %s", exc)

    async def _flush_io(self, mono: float) -> None:
        if self._raw_pending:
            items, self._raw_pending = self._raw_pending, []
            await asyncio.to_thread(self._write_raw, items)
        if self._save_due(mono):
            await self._save_async(mono=mono)

    # ---- reading ----------------------------------------------------------------

    def latest(self, now: Optional[float] = None, mono: Optional[float] = None) -> Reading:
        now = time.time() if now is None else now
        mono = time.monotonic() if mono is None else mono
        r = self.tracker.reading(now, self.cfg.meter.stale_after_s, mono)
        if r.stale:
            extra = [e for e in (self.last_error,) if e]
            if r.recovery_step != "none":
                extra.append(f"recovery: {r.recovery_step}")
            if extra:
                r.error = "; ".join(([r.error] if r.error else []) + extra)
        return r

    def handle_line(self, line: str, now: Optional[float] = None,
                    mono: Optional[float] = None) -> bool:
        """Process one stdout line from rtl_433. True if it was an accepted packet.

        No disk I/O here: raw-log lines are queued for _flush_io().
        """
        line = line.strip()
        if not line:
            return False
        try:
            pkt = json.loads(line)
        except ValueError:
            log.debug("rtl_433 non-JSON output: %s", line[:200])
            return False
        if not isinstance(pkt, dict):
            return False
        now = time.time() if now is None else now
        mono = time.monotonic() if mono is None else mono
        accepted = self.tracker.ingest(pkt, now, mono)
        result = self.tracker.last_result
        if result == "foreign":
            return False
        self._on_heard()
        if result in ("accepted", "rejected") and cfg_get(self.cfg, "storage", "raw_log"):
            self._raw_pending.append((now, pkt))
        if accepted:
            log.debug("packet: volume=%s cu ft snr=%s rssi=%s", self.tracker.volume,
                      self.tracker.snr, self.tracker.rssi)
            self.last_error = None
        return accepted

    # ---- recovery ladder --------------------------------------------------------

    def _enabled(self, step: str) -> bool:
        if step == "restart":
            return True
        return bool(cfg_get(self.cfg, "recovery", step))

    def _on_heard(self) -> None:
        t = self.tracker
        if t.recovery_step != "none":
            log.warning("meter %d heard again; recovery ladder reset (was at %s)",
                        t.meter_id, t.recovery_step)
            t.recovery_step = "none"
        self._backoff = EXHAUSTED_BACKOFF_S
        self._next_retry_mono = None

    def _next_step(self, current: str, mono: float) -> str:
        if current == "exhausted":
            if self._next_retry_mono is not None and mono < self._next_retry_mono:
                return "idle_restart"
            priv = [s for s in PRIVILEGED if self._enabled(s)]
            return priv[0] if priv else "restart"
        if current in LADDER:
            for s in LADDER[LADDER.index(current) + 1:]:
                if self._enabled(s):
                    return s
            return "exhausted"
        return "restart"

    def _ladder_tick(self, now: float, mono: float) -> Optional[str]:
        """Start the next ladder step if one is due. Returns the step started."""
        task = self._step_task
        if task is not None:
            if not task.done():
                return None
            self._step_task = None
            if not task.cancelled() and task.exception() is not None:
                log.error("recovery step failed", exc_info=task.exception())
        if self._unkillable_pending:
            self._unkillable_pending = False
            target = next((s for s in PRIVILEGED if self._enabled(s)), None)
            log.error("rtl_433 is unkillable; jumping straight to %s",
                      target or "(no privileged step enabled)")
            if target is None:
                return None
            self._ladder_ref = mono
            self._step_task = asyncio.create_task(self._do_step(target, now, mono, stop_child=False))
            return target
        heard = self.tracker.last_heard_mono
        ref = self._ladder_ref if heard is None else max(heard, self._ladder_ref)
        if mono - ref < float(self.cfg.source.restart_if_silent_s):
            return None
        step = self._next_step(self.tracker.recovery_step, mono)
        self._ladder_ref = mono  # next step only after another full silent period
        self._step_task = asyncio.create_task(self._do_step(step, now, mono))
        return step

    def _silence_text(self, mono: float) -> str:
        heard = self.tracker.last_heard_mono
        if heard is None:
            return "since this service started"
        return f"for {mono - heard:.0f}s"

    async def _do_step(self, step: str, now: float, mono: float, stop_child: bool = True) -> None:
        self._ensure_async()
        t = self.tracker
        mid = t.meter_id
        if step == "idle_restart":
            log.warning("no packet from meter %d %s; recovery exhausted, restarting rtl_433 "
                        "only (next escalation in %.1f h)", mid, self._silence_text(mono),
                        (self._next_retry_mono - mono) / 3600.0)
            if not await self._stop_child("ladder"):
                await self._jump_unkillable(now, mono)
            return
        if step in ("restart", "exhausted"):
            t.recovery_step = step
            if step == "restart":
                log.warning("RECOVERY restart: no packet from meter %d %s; restarting rtl_433 "
                            "(dongle may be wedged)", mid, self._silence_text(mono))
            else:
                delay = self._backoff
                self._next_retry_mono = mono + delay
                self._backoff = min(delay * 2, EXHAUSTED_BACKOFF_MAX_S)
                log.error("RECOVERY EXHAUSTED: meter %d silent %s through every enabled step "
                          "(usb_resets=%d power_cycles=%d reboots_requested=%d); rtl_433 will "
                          "still be restarted, privileged steps retried in %.0f h",
                          mid, self._silence_text(mono), t.usb_resets, t.power_cycles,
                          t.reboots_requested, delay / 3600.0)
            await self._save_async(now, mono)
            if not await self._stop_child("ladder"):
                await self._jump_unkillable(now, mono)
            return
        await self._privileged(step, now, mono, stop_child)

    async def _jump_unkillable(self, now: float, mono: float) -> None:
        self._unkillable_pending = False
        target = next((s for s in PRIVILEGED if self._enabled(s)), None)
        log.error("rtl_433 is unkillable; jumping straight to %s",
                  target or "(no privileged step enabled)")
        if target is not None:
            await self._privileged(target, now, mono, stop_child=False)

    async def _privileged(self, step: str, now: float, mono: float, stop_child: bool) -> None:
        t = self.tracker
        t.recovery_step = step
        if step == "usb_reset":
            t.usb_resets += 1
            log.warning("RECOVERY usb_reset #%d: meter %d silent %s; asking the root helper for "
                        "USBDEVFS_RESET on the dongle", t.usb_resets, t.meter_id,
                        self._silence_text(mono))
        elif step == "power_cycle":
            t.power_cycles += 1
            log.error("RECOVERY power_cycle #%d: meter %d silent %s; asking the root helper to "
                      "cut USB power (uhubctl)", t.power_cycles, t.meter_id,
                      self._silence_text(mono))
        else:
            t.reboots_requested += 1
            log.error("RECOVERY reboot #%d: meter %d silent %s; asking the root helper to "
                      "REBOOT the Pi", t.reboots_requested, t.meter_id, self._silence_text(mono))
        self._ensure_async()
        self._may_spawn.clear()  # keep rtl_433 off the dongle while it is reset
        try:
            if stop_child:
                await self._stop_child("ladder")
            self._unkillable_pending = False  # this step is the remedy
            await self._save_async(now, mono)
            ok, detail = await self._helper_request(step)
            if ok:
                log.warning("RECOVERY %s done: %s", step, detail)
            else:
                log.error("RECOVERY %s FAILED: %s (counted; the ladder moves on next "
                          "silent period)", step, detail)
        finally:
            self._may_spawn.set()

    # ---- privileged helper client -----------------------------------------------

    def _write_request(self, req: dict) -> None:
        self.helper_dir.mkdir(parents=True, exist_ok=True)
        try:
            (self.helper_dir / "response.json").unlink()
        except FileNotFoundError:
            pass
        atomic_write_json(self.helper_dir / "request.json", req)

    def _read_response(self, rid: str) -> Optional[dict]:
        p = self.helper_dir / "response.json"
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(d, dict) or d.get("id") != rid:
            return None
        try:
            p.unlink()
        except OSError:
            pass
        return d

    def _withdraw_request(self, rid: str) -> None:
        """Remove our unanswered request so it cannot fire hours later."""
        p = self.helper_dir / "request.json"
        try:
            if json.loads(p.read_text()).get("id") == rid:
                p.unlink()
        except (OSError, ValueError, AttributeError):
            pass

    async def _helper_request(self, action: str) -> tuple:
        """Ask the root helper for `action`. Returns (ok, detail); never raises."""
        rid = uuid.uuid4().hex
        timeout = float(cfg_get(self.cfg, "recovery", "step_timeout_s"))
        try:
            await asyncio.to_thread(self._write_request,
                                    {"id": rid, "action": action, "ts": time.time()})
        except OSError as exc:
            return False, f"cannot write helper request in {self.helper_dir}: {exc}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(HELPER_POLL_S)
            resp = await asyncio.to_thread(self._read_response, rid)
            if resp is not None:
                return bool(resp.get("ok")), str(resp.get("detail"))
        await asyncio.to_thread(self._withdraw_request, rid)
        return False, (f"no answer from the root helper in {timeout:g}s "
                       "(is water-bacnet-helper.path enabled?)")

    # ---- process management -----------------------------------------------------

    async def _log_version(self) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cfg.source.rtl433_path, "-V",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            log.warning("could not run '%s -V': %s", self.cfg.source.rtl433_path, exc)
            return
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 10)
            text = out.decode(errors="replace").strip().splitlines()
            log.info("rtl_433 version: %s", text[0] if text else "(no output)")
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("could not run '%s -V': %r", self.cfg.source.rtl433_path, exc)
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        while True:
            try:
                line = await stream.readline()
            except (ValueError, asyncio.LimitOverrunError):
                log.info("rtl_433: (over-long stderr line skipped)")
                continue
            except Exception:
                log.exception("rtl_433 stderr reader failed")
                return
            if not line:
                return
            text = line.decode(errors="replace").rstrip()
            if text:
                log.info("rtl_433: %s", text)

    async def _read_stdout(self, stream: asyncio.StreamReader) -> bool:
        """Read packets until EOF. Returns True if any packet was accepted.

        asyncio readline only suspends this coroutine, so the watchdog and the BACnet
        side keep running while we wait minutes between packets.
        """
        while True:
            try:
                line = await stream.readline()
            except (ValueError, asyncio.LimitOverrunError):
                log.warning("rtl_433 emitted an over-long line; skipping")
                continue
            if not line:
                return self._got
            mono = time.monotonic()
            try:
                if self.handle_line(line.decode(errors="replace"), time.time(), mono):
                    self._got = True
                await self._flush_io(mono)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("error handling rtl_433 output; continuing")

    async def _reap(self, proc, timeout: float) -> bool:
        """Wait up to timeout for proc to have exited (polls returncode)."""
        deadline = time.monotonic() + timeout
        while proc.returncode is None:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    def _abandon(self, proc) -> None:
        log.error("rtl_433 pid %s did not exit %.0fs after SIGKILL (stuck in the kernel, "
                  "D state?); abandoning it", getattr(proc, "pid", "?"), UNKILLABLE_S)
        self._abandoned.append(proc)
        self._unkillable_pending = True
        if self._read_task is not None and self.proc is proc:
            self._read_task.cancel()

    async def _stop_child(self, reason: str) -> bool:
        """SIGKILL rtl_433 and wait for it. False = unkillable (abandoned)."""
        proc = self.proc
        if proc is None:
            return True
        if any(p is proc for p in self._abandoned):
            return False
        if proc.returncode is not None:
            return True
        self._kill_reason = reason
        try:
            proc.kill()
        except ProcessLookupError:
            return True
        if await self._reap(proc, UNKILLABLE_S):
            return True
        self._abandon(proc)
        return False

    def _kill(self) -> None:
        proc = self.proc
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def _run_once(self) -> bool:
        """Run rtl_433 until it exits (or is abandoned). True if it delivered a packet."""
        cmd = self.command()
        log.info("starting: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.proc = proc
        self._got = False
        stderr_task = asyncio.create_task(self._drain_stderr(proc.stderr))
        read_task = asyncio.create_task(self._read_stdout(proc.stdout))
        self._read_task = read_task
        rc = None
        try:
            await asyncio.wait({read_task})
            if not read_task.cancelled():
                exc = read_task.exception()
                if exc is not None:
                    log.error("rtl_433 stdout reader failed", exc_info=exc)
                # EOF: the process normally exits with it; make sure it does.
                if not await self._reap(proc, 2.0):
                    self._kill()
                    if not await self._reap(proc, UNKILLABLE_S):
                        self._abandon(proc)
            rc = proc.returncode
        finally:
            if proc not in self._abandoned:
                self._kill()
            read_task.cancel()
            stderr_task.cancel()
            self._read_task = None
        self.last_error = (f"rtl_433 exited rc={rc}" if rc is not None
                           else "rtl_433 stuck in the kernel (unkillable)")
        return self._got

    async def _watchdog_once(self, now: Optional[float] = None,
                             mono: Optional[float] = None) -> None:
        """One watchdog iteration: stale logging, ladder, periodic save and prune."""
        now = time.time() if now is None else now
        mono = time.monotonic() if mono is None else mono
        r = self.latest(now, mono)
        if r.stale != self._was_stale:
            if r.stale:
                log.warning("feed STALE: %s", r.error)
            elif self._was_stale is not None:
                log.info("feed fresh again (age %.0fs)", r.seconds_since_packet or 0)
            self._was_stale = r.stale
        self._ladder_tick(now, mono)
        if self._save_due(mono):
            await self._save_async(now, mono)
        if self._last_prune_mono is None or mono - self._last_prune_mono >= PRUNE_EVERY_S:
            self._last_prune_mono = mono
            days = cfg_get(self.cfg, "storage", "raw_retention_days")
            await asyncio.to_thread(prune_raw, self.raw_dir, days, now)

    async def _watchdog(self) -> None:
        """Supervised: an exception is logged and the loop carries on."""
        while True:
            await asyncio.sleep(WATCHDOG_S)
            try:
                await self._watchdog_once()
                self._heartbeat = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("watchdog iteration failed; continuing")

    async def run(self) -> None:
        self._ensure_async()
        await self._log_version()
        watchdog = asyncio.create_task(self._watchdog())
        backoff = BACKOFF_MIN_S
        try:
            while True:
                try:
                    if watchdog.done():
                        if not watchdog.cancelled() and watchdog.exception() is not None:
                            log.error("watchdog task died; restarting it",
                                      exc_info=watchdog.exception())
                        watchdog = asyncio.create_task(self._watchdog())
                    await self._may_spawn.wait()
                    started = time.monotonic()
                    self._kill_reason = None
                    try:
                        got = await self._run_once()
                    except OSError as exc:  # e.g. rtl_433 not installed
                        got = False
                        self.last_error = f"cannot start rtl_433: {exc}"

                    self.tracker.rtl433_restarts += 1
                    if self._kill_reason is not None:
                        delay = RESTART_DELAY_S  # deliberate restart: no backoff
                        log.info("rtl_433 stopped by the recovery ladder; restart #%d",
                                 self.tracker.rtl433_restarts)
                    else:
                        if got or time.monotonic() - started >= HEALTHY_RUN_S:
                            backoff = BACKOFF_MIN_S
                        delay = backoff
                        backoff = min(backoff * 2, BACKOFF_MAX_S)
                        log.warning("%s; restart #%d in %.0fs",
                                    self.last_error, self.tracker.rtl433_restarts, delay)
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("rtl_433 supervisor error; retrying in %.0fs", BACKOFF_MAX_S)
                    await asyncio.sleep(BACKOFF_MAX_S)
        finally:
            watchdog.cancel()
            if self._step_task is not None:
                self._step_task.cancel()
            self._kill()
            if self.proc is not None and self.proc not in self._abandoned:
                try:
                    await self._reap(self.proc, 5)
                except asyncio.CancelledError:
                    pass
            if self._raw_pending:
                items, self._raw_pending = self._raw_pending, []
                self._write_raw(items)
            self.save_state()
            log.info("state saved to %s", self.state_path)


class StateFileSource:
    """Follow an existing collector's state.json (e.g. an existing SDR collector on another host)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.path = Path(cfg.source.state_file)
        self._good: Optional[dict] = None  # last successfully parsed values
        self._good_updated: Optional[float] = None
        self._read_error: Optional[str] = None
        self._status: Optional[str] = None  # last logged status category
        self._heartbeat = time.monotonic()

    def heartbeat(self) -> float:
        return self._heartbeat

    def _parse(self) -> dict:
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            raise ValueError("state file missing")
        except OSError as exc:
            raise ValueError(f"state file unreadable: {exc.strerror or exc}")
        except ValueError:
            raise ValueError("state file is not valid text")
        try:
            d = json.loads(text)
        except ValueError:
            raise ValueError("state file is not valid JSON")
        if not isinstance(d, dict):
            raise ValueError("state file is not a JSON object")
        try:
            d["_updated_epoch"] = parse_iso(d["updated"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError("state file has no valid 'updated'")
        if _number(d.get("volume")) is None:
            raise ValueError("state file has no numeric 'volume'")
        mid = d.get("meter_id")
        if mid is not None and _int(mid, -1) != self.cfg.meter.id:
            raise ValueError(f"state file is for meter {mid}, not {self.cfg.meter.id}")
        return d

    def _read(self) -> tuple:
        """(parsed dict, None) or (None, error text). Never raises; may block (thread)."""
        try:
            return self._parse(), None
        except ValueError as exc:
            return None, str(exc)
        except Exception as exc:  # parseable-but-wrong must not crash the service
            return None, f"state file unusable: {type(exc).__name__}: {exc}"

    def poll(self, now: Optional[float] = None, result: Optional[tuple] = None) -> Reading:
        """Re-read the file once; logs only on status transitions. Never raises."""
        now = time.time() if now is None else now
        d, err = self._read() if result is None else result
        if d is not None:
            self._good = d
            self._good_updated = d["_updated_epoch"]
            self._read_error = None
        else:
            self._read_error = err
        try:
            r = self.latest(now)
        except Exception as exc:
            log.exception("state file values unusable")
            self._good = None
            r = Reading.empty(f"state file values unusable: {exc}")
        status = "fresh" if not r.stale else (self._read_error or "stale-age")
        if status != self._status:
            if r.stale:
                log.warning("feed STALE: %s", r.error)
            elif self._status is not None:
                log.info("feed fresh again (age %.0fs)", r.seconds_since_packet or 0)
            else:
                log.info("feed fresh (age %.0fs)", r.seconds_since_packet or 0)
            self._status = status
        return r

    def latest(self, now: Optional[float] = None) -> Reading:
        now = time.time() if now is None else now
        if self._good is None:
            return Reading.empty(self._read_error or "state file not read yet")
        d = self._good
        updated = self._good_updated
        file_age = now - updated
        ssp = _number(d.get("seconds_since_packet"))
        # The file's seconds_since_packet is as of `updated`; add the time since then
        # so a frozen collector still shows a growing age.
        age = None if ssp is None else ssp + max(0.0, file_age)
        stale_after = self.cfg.meter.stale_after_s
        error = None
        if self._read_error:
            error = self._read_error
        elif file_age > stale_after:
            error = f"state file not updated for {file_age:.0f}s"
        elif age is None:
            error = "no packet received yet"
        elif age > stale_after:
            error = f"no packet for {age:.0f}s"
        return Reading(
            volume_cuft=_number(d.get("volume")),  # cubic feet, see meter.py
            flow_per_hour=_number(d.get("flow_per_hour")),
            flow_recent_per_hour=_number(d.get("flow_recent_per_hour")),
            seconds_since_packet=age,
            snr=_number(d.get("snr")),
            rssi=_number(d.get("rssi")),
            freq=_number(d.get("freq")),
            packets_per_hour=_number(d.get("packets_per_hour")),
            rtl433_restarts=_int(d.get("rtl433_restarts"), 0),
            packets_total=_int(d.get("packets_total"), 0),
            stale=error is not None,
            error=error,
            updated=updated,
        )

    async def run(self) -> None:
        while True:
            try:
                # File I/O in a thread; state is updated on the event loop.
                self.poll(result=await asyncio.to_thread(self._read))
                self._heartbeat = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("state file poll failed; continuing")
            await asyncio.sleep(self.cfg.bacnet.refresh_s)
