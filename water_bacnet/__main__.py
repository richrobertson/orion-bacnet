"""Entry point: python -m water_bacnet [--config PATH] [--check]"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from . import __version__, sdnotify
from .config import DEFAULT_CONFIG_PATH, load_config, loads_config, meter_id_problem
from .sources import Rtl433Source, StateFileSource

log = logging.getLogger("water_bacnet")


def _setup_logging() -> None:
    level_name = os.environ.get("WATER_BACNET_LOG", "INFO").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.INFO
    # No timestamp: the journal adds its own.
    logging.basicConfig(
        stream=sys.stderr, level=level, format="%(levelname)s %(name)s: %(message)s"
    )


def make_source(cfg):
    if cfg.source.mode == "state_file":
        return StateFileSource(cfg)
    return Rtl433Source(cfg)


def _check(cfg) -> int:
    problem = meter_id_problem(cfg)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    source = make_source(cfg)
    now = time.time()
    if isinstance(source, StateFileSource):
        reading = source.poll(now)
        out = {"mode": "state_file", "state_file": cfg.source.state_file,
               "reading": dataclasses.asdict(reading)}
        print(json.dumps(out, indent=2))
        return 1 if reading.stale else 0

    # rtl_433 mode: we do not open the radio here (the service may own it); show the
    # config, the command line and whatever reading the saved state gives.
    binary = shutil.which(cfg.source.rtl433_path)
    out = {
        "mode": "rtl_433",
        "config": cfg.to_dict(),
        "command": source.command(),
        "rtl433_found": binary,
        "saved_state": str(source.state_path),
        "reading": dataclasses.asdict(source.latest(now)),
    }
    print(json.dumps(out, indent=2))
    if binary is None:
        print(f"error: {cfg.source.rtl433_path!r} not found on PATH", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------------
# Config reload watcher + health heartbeat (for the web portal, water_bacnet.portal).
#
# - The portal never needs root: it rewrites config.toml, this watcher sees the file
#   change and exits 0, and the unit's Restart=always starts us again on the new file.
# - A config.toml that fails to load falls back to config.toml.last-good (logged as an
#   ERROR), so a broken file never keeps the service down.
# - A small JSON heartbeat in the runtime directory (/run/water-bacnet, tmpfs, so no SD
#   card wear) tells the portal which config file we run on and whether we are alive;
#   the portal uses it to confirm or roll back a change.

RELOAD_POLL_S = 2.0
HEARTBEAT_S = 5.0
STATUS_FILE = "main-status.json"
# "ready" = BACnet is actually serving, not just "the process is alive": bacnet.py may
# wait up to 60 s for a bind address and 15 s for its sockets before it can fail.
# bacnet.serve() calls Supervisor.mark_ready() (the on_ready hook) at the same moment it
# sends READY=1 to systemd. The heartbeat says so with "ready_via": "signal"; a portal
# reading a heartbeat WITHOUT that key is talking to an older main and falls back to
# "alive for 80 s".
READY_VIA = "signal"


def last_good_path(path) -> str:
    return str(path) + ".last-good"


def _read_config(path):
    data = Path(path).read_bytes()
    return loads_config(data.decode("utf-8"), str(path)), hashlib.sha256(data).hexdigest()


def load_with_fallback(path):
    """-> (cfg, loaded_from "config"|"last-good", sha256, error). Raises the ORIGINAL
    error if neither config.toml nor config.toml.last-good loads."""
    try:
        cfg, sha = _read_config(path)
        return cfg, "config", sha, None
    except (OSError, ValueError) as exc:
        lg = last_good_path(path)
        try:
            cfg, sha = _read_config(lg)
        except (OSError, ValueError):
            raise exc
        log.error("bad config %s: %s -- RUNNING ON THE LAST-GOOD COPY %s until it is fixed",
                  path, exc, lg)
        return cfg, "last-good", sha, str(exc)


def _file_sig(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _runtime_dir() -> Optional[str]:
    # systemd sets RUNTIME_DIRECTORY from RuntimeDirectory= (colon-separated list).
    d = os.environ.get("RUNTIME_DIRECTORY") or os.environ.get("WATER_BACNET_RUNTIME_DIR")
    return d.split(":")[0] if d else None


class Supervisor:
    """Watches the config file and writes the heartbeat. Never raises."""

    def __init__(self, config_path, loaded_from: str, sha: Optional[str],
                 error: Optional[str] = None, runtime_dir: Optional[str] = None,
                 poll_s: float = RELOAD_POLL_S):
        self.config_path = str(config_path)
        self.loaded_from = loaded_from
        self.sha = sha
        self.error = error
        self.runtime_dir = runtime_dir if runtime_dir is not None else _runtime_dir()
        self.poll_s = poll_s
        self.started = time.time()
        self.ready_at: Optional[float] = None
        self.bacnet_address: Optional[str] = None
        self.health = None  # HealthMonitor, for the heartbeat (optional)
        self.reload_requested = False
        self._sig = _file_sig(self.config_path)

    def write(self, state: str, error: Optional[str] = None) -> None:
        if not self.runtime_dir:
            return
        d = {
            "pid": os.getpid(), "state": state, "version": __version__,
            "config_path": self.config_path, "loaded_from": self.loaded_from,
            "config_sha256": self.sha, "config_error": self.error,
            "started": self.started, "ready_at": self.ready_at, "ready_via": READY_VIA,
            "bacnet_address": self.bacnet_address, "heartbeat": time.time(),
            "error": error,
        }
        if self.health is not None:
            try:
                d["health"] = self.health.latest().as_dict()
            except Exception:
                pass
        try:
            path = Path(self.runtime_dir) / STATUS_FILE
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(d))
            os.replace(tmp, path)
        except OSError as exc:
            log.debug("cannot write heartbeat: %s", exc)

    async def heartbeat(self) -> None:
        while True:
            self.write("running")
            await asyncio.sleep(HEARTBEAT_S)

    def mark_ready(self, info: Optional[dict] = None) -> None:
        """bacnet.serve()'s on_ready hook: the device is bound and serving."""
        if self.ready_at is None:
            self.ready_at = time.time()
        self.bacnet_address = (info or {}).get("address")
        self.write("running")

    async def watch(self, stop: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(self.poll_s)
            sig = _file_sig(self.config_path)
            if sig != self._sig:
                await asyncio.sleep(1.0)  # let a non-atomic editor finish writing
                log.info("%s changed; exiting so systemd restarts us on the new config",
                         self.config_path)
                self.reload_requested = True
                stop.set()
                return


async def _serve(cfg, sup: Optional[Supervisor] = None) -> int:
    # Imported lazily so --check works without bacpypes3 installed.
    from .bacnet import serve
    from .health import HealthMonitor

    source = make_source(cfg)
    health = HealthMonitor(cfg.health)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    source_task = asyncio.create_task(source.run(), name="source")
    bacnet_task = asyncio.create_task(
        serve(cfg, source, health=health, on_ready=sup.mark_ready if sup else None),
        name="bacnet")
    stop_task = asyncio.create_task(stop.wait(), name="stop")
    # The health poller never raises; it is not a reason to stop the service.
    extra = [asyncio.create_task(health.run(), name="health")]
    if sup is not None:
        sup.health = health
        extra += [asyncio.create_task(sup.heartbeat(), name="heartbeat"),
                  asyncio.create_task(sup.watch(stop), name="watch")]
    done, _ = await asyncio.wait(
        {source_task, bacnet_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )
    sdnotify.stopping()
    for task in extra:
        task.cancel()
    await asyncio.gather(*extra, return_exceptions=True)

    rc = 0
    failure = None
    for task in (source_task, bacnet_task):
        if task in done and not task.cancelled() and task.exception() is not None:
            log.error("%s task failed", task.get_name(), exc_info=task.exception())
            rc = 1
            failure = f"{task.get_name()}: {task.exception()!r}"
    if stop_task in done:
        log.info("shutting down")
    if sup is not None:
        sup.write("failed" if rc else "stopped", failure)

    # Cancelling the source makes it persist state in its finally block.
    for task in (source_task, bacnet_task, stop_task):
        task.cancel()
    await asyncio.gather(source_task, bacnet_task, stop_task, return_exceptions=True)
    return rc


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="water_bacnet", description="BACnet/IP server for a Badger ORION water meter"
    )
    parser.add_argument("--config", help=f"config file (default {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--check", action="store_true",
                        help="read the source once, print the reading as JSON, exit 0/1")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    _setup_logging()

    if not args.check:
        return _run_service(args.config)

    try:
        if args.config:
            cfg = load_config(args.config)
        elif os.path.exists(DEFAULT_CONFIG_PATH):
            cfg = load_config(DEFAULT_CONFIG_PATH)
        else:
            log.warning("%s not found; using built-in defaults", DEFAULT_CONFIG_PATH)
            cfg = load_config(None)
    except (OSError, ValueError) as exc:
        log.error("bad config: %s", exc)
        return 2

    return _check(cfg)


def _run_service(config_arg: Optional[str]) -> int:
    """Run the service, with last-good fallback and the reload watcher."""
    path = config_arg or DEFAULT_CONFIG_PATH
    if config_arg or os.path.exists(path):
        try:
            cfg, loaded_from, sha, error = load_with_fallback(path)
        except (OSError, ValueError) as exc:
            log.error("bad config: %s (and no usable %s)", exc, last_good_path(path))
            Supervisor(path, "none", None, str(exc)).write("failed", f"bad config: {exc}")
            return 2
    else:
        log.warning("%s not found; using built-in defaults", DEFAULT_CONFIG_PATH)
        cfg, loaded_from, sha, error = load_config(None), "defaults", None, None
    sup = Supervisor(path, loaded_from, sha, error)
    problem = meter_id_problem(cfg)
    if problem:
        # Not a crash: the config loads (so the portal can edit it), it is just not
        # usable yet. systemd keeps retrying; the portal shows this message.
        log.error("%s", problem)
        sup.error = problem
        sup.write("failed", problem)
        return 2

    log.info("water-bacnet %s starting, source mode %s (config: %s)",
             __version__, cfg.source.mode, loaded_from)
    try:
        rc = asyncio.run(_serve(cfg, sup))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        sup.write("failed", repr(exc))
        raise
    if sup.reload_requested:
        return 0  # Restart=always brings us back on the new config
    return rc


if __name__ == "__main__":
    sys.exit(main())
