"""Root oneshot that performs privileged recovery steps for the unprivileged service.

    python -m water_bacnet.helper [--config P] [--request-dir D] [--ledger L]

Started by water-bacnet-helper.path whenever /var/lib/water-bacnet/helper/request.json
exists. Protocol (see docs/DESIGN.md, "Privileged helper"):

  request.json  (written atomically by the service)  {"id", "action", "ts"}
  response.json (written atomically by this helper)  {"id", "ok", "detail", "ts"}

Security model: the request directory belongs to the unprivileged service, so the
request is untrusted. Only `action` (checked against a whitelist) and `id` (echoed back,
sanitised) are read from it. Every parameter -- vid:pid, dongle serial, uhubctl path,
hub location/port, delay, enable flags, rate limits -- comes from the root-owned
/etc/water-bacnet/config.toml, and the rate limits are enforced from a root-owned
ledger, so a compromised service can at most ask for the whitelisted actions as often
as the ledger allows.

Re-trigger loop: the path unit fires again as long as request.json exists, so the
request is ALWAYS removed (valid) or renamed to request.json.bad-<ts> (unparseable,
not whitelisted) before any action, even on errors.

Logs go to stderr, i.e. the journal.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import stat
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from . import usbctl

log = logging.getLogger("water_bacnet.helper")

DEFAULT_CONFIG = "/etc/water-bacnet/config.toml"
DEFAULT_REQUEST_DIR = "/var/lib/water-bacnet/helper"
DEFAULT_LEDGER = "/var/lib/water-bacnet-helper/ledger.json"

WHITELIST = ("usb_reset", "power_cycle", "reboot")
USB_RESET_MIN_INTERVAL_S = 600.0
DAY_S = 86400.0
WEEK_S = 7 * DAY_S
MAX_REQUEST_BYTES = 4096
KEEP_BAD = 10

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_VIDPID = re.compile(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}")
_HUB_LOC = re.compile(r"[0-9]+(-[0-9]+(\.[0-9]+)*)?")
_HUB_PORT = re.compile(r"[0-9]+(,[0-9]+)*")


# ---- settings (from the root-owned config, never from the request) ----------------

def load_settings(path: str) -> SimpleNamespace:
    """[recovery] (+ [source] device for the dongle serial) with defaults.

    An unreadable or invalid config falls back to the defaults (reboot stays OFF), and
    each bad value falls back individually, so recovery keeps working with a typo.
    """
    data: dict = {}
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        log.warning("config %s missing; using defaults", path)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.error("config %s unreadable (%s); using defaults", path, exc)
    rec = data.get("recovery") if isinstance(data.get("recovery"), dict) else {}
    src = data.get("source") if isinstance(data.get("source"), dict) else {}

    def get(key, check):
        default = usbctl.DEFAULTS[("recovery", key)]
        if key not in rec:
            return default
        value = rec[key]
        try:
            ok = check(value)
        except Exception:
            ok = False
        if not ok:
            log.error("config [recovery] %s = %r is invalid; using %r", key, value, default)
            return default
        return value

    def is_bool(v):
        return isinstance(v, bool)

    def int_range(lo, hi):
        return lambda v: isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi

    def text(regex):
        return lambda v: isinstance(v, str) and regex.fullmatch(v) is not None

    device = src.get("device") if isinstance(src.get("device"), str) else ""
    serial = device[1:] if device.startswith(":") and len(device) > 1 else None
    return SimpleNamespace(
        usb_reset=get("usb_reset", is_bool),
        power_cycle=get("power_cycle", is_bool),
        reboot=get("reboot", is_bool),
        uhubctl_path=get("uhubctl_path", lambda v: isinstance(v, str) and v and
                         "\n" not in v and not v.startswith("-")),
        hub_location=get("hub_location", text(_HUB_LOC)),
        hub_port=get("hub_port", lambda v: v == "" or text(_HUB_PORT)(v)),
        cycle_delay_s=get("cycle_delay_s", int_range(1, 60)),
        usb_vid_pid=get("usb_vid_pid", text(_VIDPID)),
        max_power_cycles_per_day=get("max_power_cycles_per_day", int_range(0, 48)),
        max_reboots_per_week=get("max_reboots_per_week", int_range(0, 7)),
        serial=serial,
    )


# ---- small file helpers ----------------------------------------------------------

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


def atomic_write(path: Path, data: dict, mode: int = 0o644) -> None:
    """tmp (O_EXCL, random name: safe in a directory the service controls) + rename."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            os.fchmod(fh.fileno(), mode)
            json.dump(data, fh, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


class Helper:
    def __init__(self, config_path: str = DEFAULT_CONFIG,
                 request_dir: str = DEFAULT_REQUEST_DIR,
                 ledger_path: str = DEFAULT_LEDGER,
                 ops=usbctl, clock=time.time):
        self.config_path = config_path
        self.request_dir = Path(request_dir)
        self.request = self.request_dir / "request.json"
        self.response = self.request_dir / "response.json"
        self.ledger_path = Path(ledger_path)
        self.ops = ops
        self.clock = clock

    # ---- request intake ----------------------------------------------------------

    def _read_request(self) -> bytes:
        fd = os.open(str(self.request), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("request is not a regular file")
            data = os.read(fd, MAX_REQUEST_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_REQUEST_BYTES:
            raise ValueError("request too large")
        return data

    def _discard(self, bad: bool) -> None:
        """Remove (valid) or rename to .bad-<ts> (invalid). Never leaves request.json."""
        try:
            if bad:
                dest = self.request_dir / f"request.json.bad-{int(self.clock())}"
                os.replace(self.request, dest)
                log.warning("kept the rejected request as %s", dest.name)
                self._prune_bad()
                return
            os.unlink(self.request)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            log.error("cannot %s %s: %s", "rename" if bad else "remove", self.request, exc)
        try:
            os.unlink(self.request)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.critical("cannot remove %s (%s); the path unit will re-trigger until "
                         "its start limit stops it", self.request, exc)

    def _prune_bad(self) -> None:
        try:
            bad = sorted(self.request_dir.glob("request.json.bad-*"),
                         key=lambda p: p.lstat().st_mtime)
        except OSError:
            return
        for p in bad[:-KEEP_BAD]:
            try:
                p.unlink()
            except OSError:
                pass

    def _respond(self, rid: Optional[str], ok: bool, detail: str) -> None:
        msg = {"id": rid, "ok": bool(ok), "detail": str(detail)[:500], "ts": self.clock()}
        try:
            atomic_write(self.response, msg)
        except OSError as exc:
            log.error("cannot write %s: %s", self.response, exc)

    # ---- ledger / rate limits -------------------------------------------------

    def _load_ledger(self) -> dict:
        try:
            d = json.loads(self.ledger_path.read_text())
            if not isinstance(d, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.error("ledger %s unreadable (%s); starting a new one", self.ledger_path, exc)
            return {}
        out = {}
        for action in WHITELIST:
            items = d.get(action)
            if isinstance(items, list):
                out[action] = [float(t) for t in items
                               if isinstance(t, (int, float)) and not isinstance(t, bool)
                               and t == t and abs(t) < 1e12]
        return out

    def _limit(self, action: str, s) -> tuple:
        """(max count, window seconds) for an action."""
        if action == "usb_reset":
            return 1, USB_RESET_MIN_INTERVAL_S
        if action == "power_cycle":
            return int(s.max_power_cycles_per_day), DAY_S
        return int(s.max_reboots_per_week), WEEK_S

    def _check_and_record(self, action: str, s, now: float) -> Optional[str]:
        """None if allowed (and recorded in the ledger), else the refusal reason."""
        ledger = self._load_ledger()
        limit, window = self._limit(action, s)
        # Entries "in the future" (clock stepped back) still count while they are
        # within one window of now; anything further out is impossible and dropped.
        for a in list(ledger):
            ledger[a] = [t for t in ledger[a] if now - WEEK_S < t <= now + WEEK_S]
        recent = [t for t in ledger.get(action, []) if now - window < t <= now + window]
        if len(recent) >= limit:
            return (f"rate limited: {len(recent)} {action} in the last {window / 3600:.0f} h "
                    f"(max {limit})")
        ledger.setdefault(action, []).append(now)
        try:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.ledger_path, ledger, mode=0o600)
        except OSError as exc:
            # Fail closed: an action we cannot account for is an action we do not do.
            return f"cannot record in ledger {self.ledger_path}: {exc}"
        return None

    # ---- actions -----------------------------------------------------------------

    def _perform(self, action: str, s) -> str:
        if action == "usb_reset":
            bus, dev = self.ops.find_dongle(s.usb_vid_pid, s.serial)
            return self.ops.usb_reset(bus, dev)
        if action == "power_cycle":
            return self.ops.power_cycle(s.uhubctl_path, s.hub_location, s.hub_port,
                                        s.cycle_delay_s)
        raise AssertionError(action)

    def run(self) -> int:
        try:
            os.lstat(self.request)
        except FileNotFoundError:
            log.info("no request at %s; nothing to do", self.request)
            return 0
        except OSError as exc:
            log.error("cannot stat %s: %s", self.request, exc)
            return 1

        rid: Optional[str] = None
        try:
            try:
                req = json.loads(self._read_request())
                if not isinstance(req, dict):
                    raise ValueError("request is not a JSON object")
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                log.error("rejecting unreadable request: %s", exc)
                self._discard(bad=True)
                self._respond(None, False, f"bad request: {exc}")
                return 0
            raw_id = req.get("id")
            rid = raw_id if isinstance(raw_id, str) and _ID.fullmatch(raw_id) else None
            action = req.get("action")
            if rid is None or action not in WHITELIST:
                why = "missing/invalid id" if rid is None else f"action {action!r} not allowed"
                log.error("rejecting request: %s", why)
                self._discard(bad=True)
                self._respond(rid, False, f"bad request: {why}")
                return 0
            # Valid: remove it BEFORE acting (a reboot must not re-trigger at boot).
            self._discard(bad=False)
            extra = sorted(set(req) - {"id", "action", "ts"})
            if extra:
                log.warning("ignoring request fields %s (parameters come from %s only)",
                            extra, self.config_path)

            s = load_settings(self.config_path)
            if not getattr(s, action):
                log.warning("refusing %s: disabled in %s", action, self.config_path)
                self._respond(rid, False, f"{action} disabled in config")
                return 0
            now = self.clock()
            refusal = self._check_and_record(action, s, now)
            if refusal:
                log.warning("refusing %s: %s", action, refusal)
                self._respond(rid, False, refusal)
                return 0

            if action == "reboot":
                log.critical("REBOOT requested by water-bacnet (meter silent through the "
                             "whole recovery ladder)")
                self._respond(rid, True, "reboot requested")
                try:
                    self.ops.request_reboot()
                except Exception as exc:
                    log.error("reboot failed: %s", exc)
                    self._respond(rid, False, f"reboot failed: {exc}")
                return 0

            log.warning("performing %s (request %s)", action, rid)
            try:
                detail = self._perform(action, s)
            except Exception as exc:
                log.error("%s failed: %s", action, exc)
                self._respond(rid, False, f"{action} failed: {exc}")
                return 0
            log.warning("%s done: %s", action, detail)
            self._respond(rid, True, detail)
            return 0
        except Exception as exc:  # never leave request.json behind
            log.exception("helper crashed")
            self._discard(bad=True)
            self._respond(rid, False, f"helper error: {exc}")
            return 1


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(prog="water_bacnet.helper",
                                description="water-bacnet privileged recovery helper")
    p.add_argument("--config", default=os.environ.get("WATER_BACNET_HELPER_CONFIG", DEFAULT_CONFIG))
    p.add_argument("--request-dir",
                   default=os.environ.get("WATER_BACNET_HELPER_REQUEST_DIR", DEFAULT_REQUEST_DIR))
    p.add_argument("--ledger", default=os.environ.get("WATER_BACNET_HELPER_LEDGER", DEFAULT_LEDGER))
    args = p.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(levelname)s water-bacnet-helper: %(message)s")
    return Helper(args.config, args.request_dir, args.ledger).run()


if __name__ == "__main__":
    sys.exit(main())
