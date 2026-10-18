# Design

How orion-bacnet works inside, and the rules a change must keep. The README covers
installing and operating it; [HARDWARE.md](HARDWARE.md) covers the outdoor receiver.

## Purpose and scope

Expose one Badger ORION water meter as a **read-only** BACnet/IP device, so a building
automation system (Mango Automation or any BACnet client) can poll it. The meter is
read over the air: an RTL-SDR and `rtl_433` decode the ORION endpoint's 915 MHz
transmissions. Alternatively the service can follow a `state.json` written by another
collector.

Non-goals: writing anything to BACnet (every present-value is read-only), billing-grade
data (the radio protocol is unauthenticated), and more than one meter per process.

## Data facts that must be respected

- **The ORION volume field is cubic feet for these meters.** rtl_433's Badger-ORION
  decoder calls it `volume_gal`, but on the meters this was built against it is 1:1
  with the register dial in **cubic feet**, resolution 1 cu ft. Never treat it as
  gallons. AI 2 is the only gallons object: 1 cu ft = 7.480519 US gal. If your meter's
  register is in gallons, compare the dial against AI 1 before trusting AI 2.
- **The on-air id is `serial & 0xFFFFFF`**, the low 24 bits of the serial number
  stamped on the endpoint. `[meter] id` has no default; the service refuses to start
  without it. Packets are accepted only when `model == "Badger-ORION"` and
  `id == [meter] id`; neighbours' meters are ignored.
- **A flat register for hours is normal** (little water used). Staleness is judged
  only from packet age and, in `state_file` mode, the file's own `updated` time, never
  from an unchanged volume.
- **About 22 packets/hour is the healthy rate**; gaps of several minutes are normal.
- **Protocol number:** 223 in rtl_433 25.02, 219 in older builds. It is configurable
  and `install.sh` prints what the installed build calls it.
- **Physical ceiling:** a reading that implies more than ~1300 cu ft/h (~160 gpm, the
  top of a 1.5–2 in Recordall's rating) since the last good one is a bad decode and is
  rejected (see below). A larger meter needs a larger `MAX_FLOW_CUFT_H` in `meter.py`.

## Architecture

```
 RTL-SDR ──► rtl_433 (child process, JSON lines)
                │  asyncio, non-blocking reads
                ▼
         Rtl433Source ──► MeterTracker ──► Reading ──┐
         (or StateFileSource ◄── collector state.json) │
                                                       ▼
 HealthMonitor (CPU temp, throttle, SHT31/BME280) ──► bacnet.serve()
                                                       │ bacpypes3, UDP 47808
                                                       ▼
                                             BACnet client (read-only)

 water-bacnet.service (unprivileged)  ── request.json ──►  water-bacnet-helper (root oneshot)
 water-bacnet-portal.service (stdlib HTTP) ── config.toml ──► service restarts on change
```

Three processes:

1. **`water-bacnet.service`** (`python -m water_bacnet`): the source, the tracker, the
   health poller and the BACnet server in one asyncio loop. Unprivileged.
2. **`water-bacnet-portal.service`** (`python -m water_bacnet.portal`): the web
   configuration portal, stdlib only, on the system `python3` so a broken venv cannot
   take it down too.
3. **`water-bacnet-helper.service`** (root oneshot, started by a `.path` unit): the
   only privileged code. See [Privileged helper](#privileged-helper).

## Module contract

Python 3.11 compatible (Raspberry Pi OS Bookworm): no 3.12+ syntax. Runtime
dependencies: the standard library and `bacpypes3==0.0.110` (vendored, hash-checked).

```
water_bacnet/
  __init__.py   __version__
  config.py     load_config(path) -> Config (frozen dataclasses, tomllib, a default for
                every key except [meter] id); loads_config(text); problems(cfg) (the
                portal's stricter checks); meter_id_problem(cfg); dump_toml(cfg)
  meter.py      Reading (dataclass; numeric fields Optional, None = never known):
                  volume_cuft, flow_per_hour, flow_recent_per_hour, seconds_since_packet,
                  snr, rssi, freq, packets_per_hour, rtl433_restarts, packets_total,
                  stale, error, updated, usb_resets, power_cycles, reboots_requested,
                  recovery_step
                MeterTracker(meter_id, history_hours=48):
                  ingest(pkt, now) -> bool; reading(now, stale_after) -> Reading;
                  to_json() / from_json(d)  (same schema as a collector state.json)
  sources.py    Rtl433Source(cfg), StateFileSource(cfg):
                  async run(); latest(now) -> Reading; heartbeat() -> monotonic float
  bacnet.py     async serve(cfg, source, health=None, on_ready=None) -> never returns
  health.py     HealthMonitor(cfg.health): poll_once(), async run(), latest()
  sdnotify.py   notify(), ready(), watchdog(), stopping(), watchdog_interval()
  usbctl.py     find_dongle(), USBDEVFS_RESET, uhubctl power cycle (used by the helper)
  helper.py     the root helper (python -m water_bacnet.helper)
  portal.py     the web portal (python -m water_bacnet.portal)
  __main__.py   python -m water_bacnet [--config PATH] [--check]
```

`--check` runs the source once (state_file: read; rtl_433: print the config, the
command line and the saved state; it does not open the radio) and prints the Reading as
JSON. It exits 2 on a bad config or an unset meter id, 1 when the feed is stale
(state_file mode) or the rtl_433 binary is not found (rtl_433 mode), else 0.

## The rtl_433 source

- Command: `rtl_433 [-d <device>] -f 916.45M -s 1600k -R 223 -F json -M level
  -M time:iso` plus `extra_args`; every part is configurable. `device` defaults to the
  first dongle; pinning it by serial (`":<serial>"`) is recommended.
- 🔑 **stdout is read asynchronously, never with a blocking readline.** Packets are
  minutes apart, and a blocked reader froze the age counter in an earlier collector.
  stderr is drained by a supervised task.
- Derived values: `flow_per_hour` from register history over ~6 h,
  `flow_recent_per_hour` over ~1 h, `packets_per_hour` rolling 1 h,
  `seconds_since_packet`. Flow is never negative and never above the physical ceiling.
- **Clocks:** ages, silence, rates and the save throttle use the **monotonic** clock,
  so a wall-clock jump (the Pi has no RTC and steps at its first NTP sync) cannot freeze
  or fake the feed. A restored `last_packet` in the future is treated as "no packet
  yet"; history entries in the future are discarded.
- **Bad decodes:** a forward jump larger than `1300 cu ft/h × elapsed + 10 cu ft` is
  rejected. The register re-baselines when 3 consecutive packets spanning ≥ 10 minutes
  agree on a value (so a meter swap or a legitimately lower register recovers on its
  own). Dropped-but-valid packets from our id still count as "radio alive".
- **Persistence:** last values and history go to `<state_dir>/state.json` atomically
  (tmp + fsync + rename + fsync of the directory) at most every `save_interval_s`, and
  on stop. A corrupt-but-parseable file (`null`, `[]`, `1e400`) is renamed to
  `state.json.corrupt-<ts>` and the tracker starts fresh instead of crash-looping.
- **Raw log** (optional): our meter's accepted and dropped-but-valid packets in
  `raw/YYYY-MM-DD.jsonl`, pruned after `raw_retention_days`. Writes happen off the event
  loop.

## The state_file source

Re-reads a collector's `state.json` every `refresh_s` (default 15 s). Schema:

```json
{"updated": "2026-01-01T12:00:00+00:00", "meter_id": 7654321, "volume": 41774.0,
 "volume_units": "cubic_feet", "flow_per_hour": 0.0, "flow_recent_per_hour": 0.0,
 "seconds_since_packet": 221.0, "snr": 16.7, "rssi": -8.6, "freq": 915.993,
 "packets_total": 12114, "packets_per_hour": 21.65, "collector_uptime_s": 2014366,
 "rtl433_restarts": 0, "history": [[1767268800.0, 41770.0]]}
```

`seconds_since_packet` is as of `updated`, so the service reports
`seconds_since_packet + (now − updated)`: a frozen collector still shows a growing age.
A file for a different `meter_id` is an error, not data. "Missing / unreadable / bad
JSON" is distinguished from "value unchanged" and fails loudly (log + fault flags),
never silently reusing old values as healthy.

## Staleness rules

The feed is **stale** when the live packet age exceeds `stale_after_s` (default 900 s),
or (state_file mode) the file is missing, unreadable, invalid, for another meter, or
its `updated` is older than `stale_after_s`. Then:

- AI 1–9 keep their last good value but get `statusFlags` fault = true and
  `reliability = communication-failure`; BI 1 "Feed Stale" goes active.
- AI 10–11 (recovery counters) stay valid: a stale feed is exactly when they matter.
- AI 5 keeps counting up.
- Everything recovers on its own when fresh data returns.

Every transition (fresh → stale, stale → fresh, read error, rtl_433 restart, recovery
step) is logged once.

## BACnet objects

Device instance 2801 by default, vendor id 999 (unregistered). Object numbers are
**permanent**: new objects are only ever appended, never renumbered. The table is in
the README. AI 12–15 and BI 2 come from the health poller; a missing, disabled or
failing sensor reports `reliability = no-sensor` with fault = true and is logged once.

Broadcast Who-Is: binding `a.b.c.d/nn` opens a unicast socket and a subnet-broadcast
socket; on Linux the service adds a third listener on 255.255.255.255, because some
clients (BACnet4J-based ones such as Mango) send Who-Is to the limited broadcast
address. I-Am goes back unicast.

## Recovery ladder

One step per silent period (`restart_if_silent_s`, default 1800 s, minimum 300 s):

```
restart rtl_433 → usb_reset → power_cycle → reboot (only if [recovery] reboot = true) → exhausted
```

- Any accepted packet resets the ladder to `none`.
- Steps disabled in config are skipped.
- After `exhausted`, the ladder repeats from `usb_reset` at most once per 24 h, with
  the backoff doubling up to 7 days.
- An rtl_433 that is not reaped 30 s after SIGKILL (a dongle stuck in the kernel, D
  state) jumps straight to `usb_reset`.
- AI 10/11 count USB resets and power cycles (attempts).

## Privileged helper

The BACnet service has no capabilities. The privileged steps go through a root oneshot
with a deliberately narrow interface:

- Request directory `/var/lib/water-bacnet/helper/` (owned by `water-bacnet`, 0750).
  The service writes `request.json` atomically:
  `{"id": "<uuid>", "action": "usb_reset" | "power_cycle" | "reboot", "ts": <epoch>}`.
- The request carries **only the action name**. The helper checks it against a
  whitelist and takes **every parameter** (USB vid:pid, dongle serial, uhubctl path,
  hub location/port, delay, whether reboot is allowed) from the root-owned
  `/etc/water-bacnet/config.toml`, never from the request. A compromised service can
  ask for one of three fixed actions, but cannot make root run an arbitrary command or
  argument.
- The helper enforces its **own** rate limits from a root-only ledger
  (`/var/lib/water-bacnet-helper/ledger.json`): `max_power_cycles_per_day`,
  `max_reboots_per_week`, and at most one USB reset per 10 minutes. The service's own
  counters are not trusted for this.
- `water-bacnet-helper.path` (`PathExists=` the request) starts
  `water-bacnet-helper.service`, which performs the action, writes `response.json`
  `{"id", "ok", "detail", "ts"}`, and always removes the request (or renames an
  unparseable one to `request.json.bad-<ts>`) so the path unit cannot re-trigger in a
  loop. Every action is logged to the journal.

## Configuration loading

- `load_config` stays lenient about keys (unknown keys are logged and ignored) but
  **rejects** values that would make the service misbehave: non-finite numbers,
  `stale_after_s` outside 60–86400, `restart_if_silent_s` < 300, `refresh_s` outside
  1–300, `vendor_id` outside 0–65535, `cycle_delay_s` outside 1–60, a `bind` that does
  not parse. A rejected `config.toml` makes the service run on
  `config.toml.last-good`. Bad `[health]`/`[portal]` keys fall back to their own
  defaults instead.
- A config **without `[meter] id` still loads**, so the portal can start and let the
  user set it; the service and `--check` refuse to run on it (`meter_id_problem`) with
  a message explaining how to find the id.
- The portal applies the stricter `problems()` before it saves anything, then watches
  the service's heartbeat (`/run/water-bacnet/main-status.json`) and rolls back to
  last-good if the new file does not come up healthy.

## Liveness

- `Type=notify`: `READY=1` once the BACnet sockets are bound (the same moment the
  heartbeat records `"ready_via": "signal"`).
- `WatchdogSec=120`: `WATCHDOG=1` is sent from the BACnet refresh loop only while the
  source's own loop completed an iteration in the last 60 s (`source.heartbeat()`).
- The bind address is re-checked every 60 s; if it vanished, or `bind = "auto"` now
  resolves elsewhere, for more than 2 minutes, the service exits and systemd restarts
  it on the current address.
- `Restart=always`, `RestartSec=10`, `StartLimitIntervalSec=0`, `MemoryMax=256M`.
- `ExecStartPre=+venv-check.sh` rebuilds the venv offline from `vendor/` if a Debian
  Python upgrade broke it.
