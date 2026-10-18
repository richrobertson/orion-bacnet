# orion-bacnet

[![CI](https://github.com/richrobertson/orion-bacnet/actions/workflows/ci.yml/badge.svg)](https://github.com/richrobertson/orion-bacnet/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Frichrobertson%2Forion-bacnet%2Fbadges%2Fcoverage.json)](https://github.com/richrobertson/orion-bacnet/actions/workflows/ci.yml)
[![Release workflow](https://img.shields.io/github/actions/workflow/status/richrobertson/orion-bacnet/release.yml?event=push&label=release%20build)](https://github.com/richrobertson/orion-bacnet/actions/workflows/release.yml)
[![Latest release](https://img.shields.io/github/v/release/richrobertson/orion-bacnet?sort=semver&display_name=tag)](https://github.com/richrobertson/orion-bacnet/releases/latest)
[![CodeQL](https://github.com/richrobertson/orion-bacnet/actions/workflows/codeql.yml/badge.svg)](https://github.com/richrobertson/orion-bacnet/actions/workflows/codeql.yml)
[![Secret scan](https://github.com/richrobertson/orion-bacnet/actions/workflows/secrets.yml/badge.svg)](https://github.com/richrobertson/orion-bacnet/actions/workflows/secrets.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/richrobertson/orion-bacnet/badge)](https://scorecard.dev/viewer/?uri=github.com/richrobertson/orion-bacnet)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

BACnet/IP server for a Badger water meter with an **ORION** radio endpoint. It runs on a
Raspberry Pi, reads the meter over the air with an RTL-SDR and
[`rtl_433`](https://github.com/merbanan/rtl_433) (or follows an existing collector's
`state.json`), and exposes the readings as a read-only BACnet device for Mango
Automation or any other BACnet client to poll.

Pure Python + `bacpypes3==0.0.110` in a venv. No compiled dependencies.

**orion-bacnet** is the project and repository name. Inside, the Python package is
`water_bacnet` and the system user, paths and systemd units are `water-bacnet`
(`/opt/water-bacnet`, `/etc/water-bacnet`, `water-bacnet.service`, ...).

More detail: [docs/DESIGN.md](docs/DESIGN.md) (architecture, data rules, recovery
ladder, helper privilege model) and [docs/HARDWARE.md](docs/HARDWARE.md) (an outdoor
PoE receiver build, site survey and commissioning checklist).

## Status

**Field-untested on real Pi hardware.** The software has been developed and unit- and
integration-tested off-target (fake `rtl_433`, fake sysfs/I²C, real bacpypes3 over
loopback), and the decoding and data rules come from a proof-of-concept receiver on a
real meter. The Raspberry Pi package itself has not yet run on a Pi. Before relying on
it, verify on the hardware:

- [ ] `install.sh` on Raspberry Pi OS Bookworm and Trixie (arm64), including an
      upgrade re-run and `--uninstall`
- [ ] packets decode from your meter at the expected ~20 packets/hour with the Pi's
      own dongle and antenna (protocol 223 vs 219 for your rtl_433 build)
- [ ] the DVB driver blacklist and udev rule let the `water-bacnet` user open the
      dongle after a replug/reboot
- [ ] recovery: `USBDEVFS_RESET` clears a wedged dongle; `uhubctl -l 1-1 -a cycle`
      really drops VBUS on your Pi 4 revision; the dongle comes back with the same
      serial; the root helper's rate limits hold
- [ ] the hardware watchdog reboots the Pi (`echo c > /proc/sysrq-trigger`),
      `systemctl show -p WatchdogUSec water-bacnet` = 2 min, and a `kill -STOP` of the
      service gets it restarted
- [ ] health points: CPU temperature, `vcgencmd get_throttled` as the `water-bacnet`
      user under the unit's `DeviceAllow` (`/dev/vchiq`, group `video`), and the
      SHT31/BME280 enclosure readings against a reference (the BME280 humidity maths is
      checked against the datasheet formula only, not a real part)
- [ ] the persistent, capped journal (`journalctl --disk-usage`, `journalctl -b -1`
      after a reboot)
- [ ] `--unattended-upgrades`: `unattended-upgrade --dry-run -d` lists only
      Debian-Security origins
- [ ] the venv self-repair (`venv-check.sh`) after a Python upgrade
- [ ] discovery and polling from your BACnet client, both by broadcast Who-Is and by IP
- [ ] a week side by side with the meter dial (or an existing reader) to confirm the
      register units (cubic feet on the meters this was built against)

Reports from real installs are very welcome (see [CONTRIBUTING.md](CONTRIBUTING.md)).

## Install on the Pi

Raspberry Pi OS Bookworm or Trixie (arm64 or armhf), Python 3.11+.

```sh
git clone https://github.com/richrobertson/orion-bacnet
cd orion-bacnet
sudo ./install.sh
```

Or download a release tarball (`orion-bacnet-<version>.tar.gz`, with a SHA256SUMS file
and a build-provenance attestation: `gh attestation verify <file> -R
richrobertson/orion-bacnet`), unpack it and run `sudo ./install.sh` from inside. The
installer works from any directory name.

Then **set the meter id** (below): the BACnet service does not start until it is set.

What it does (it is safe to run again to upgrade):

- `apt-get install --no-install-recommends` of `python3-venv`, plus `rtl-433` and
  `uhubctl` (for the USB power-cycle recovery step, if `[recovery] power_cycle = true`)
  when the source mode is `rtl_433` (or with `--with-rtl433`). Nothing else is upgraded.
- Creates the system user `water-bacnet` (in group `plugdev` for the USB dongle, and
  `i2c` / `video` when they exist, for the enclosure sensor and `vcgencmd`).
- Creates the venv `/opt/water-bacnet/venv` **offline**: bacpypes3 0.0.110 is vendored in
  `vendor/` as its wheel, checked against `vendor/SHA256SUMS` and installed with
  `pip --no-index --require-hashes`. No internet is needed to install or to repair it.
  Copies the code to `/opt/water-bacnet/`.
- Installs `/etc/water-bacnet/config.toml` from `config.example.toml` **only if it does
  not exist yet**.
- In `rtl_433` mode: blacklists `dvb_usb_rtl28xxu` (`/etc/modprobe.d/`) and adds a udev
  rule giving RTL2832U dongles to `plugdev`. **Unplug and replug the dongle (or reboot)
  afterwards** so the DVB driver lets go of it.
- Installs, enables and starts `water-bacnet.service` and the web portal
  `water-bacnet-portal.service`, and enables the root recovery helper
  (`water-bacnet-helper.path`, see [Remote recovery](#remote-recovery-and-the-root-helper)).
  Then prints `python -m water_bacnet --check`. A failure after the files are in place
  (a unit that will not start, say) is reported as a warning and the self-check still
  runs.
- If `/dev/watchdog` exists: turns on the **hardware watchdog**
  (`/etc/systemd/system.conf.d/water-bacnet-watchdog.conf`: `RuntimeWatchdogSec=15s`,
  `RebootWatchdogSec=2min`) and re-executes systemd. On a Pi a hung kernel or PID 1 then
  reboots the box within 15 s.
- Makes the journal **persistent and capped** (`/etc/systemd/journald.conf.d/water-bacnet.conf`,
  see [Logs and SD-card wear](#logs-and-sd-card-wear)).
- With `--unattended-upgrades`: installs `unattended-upgrades` and limits it to
  **Debian-Security** with automatic reboot **off** (see [Updates](#updates)).
- Re-running it refreshes its own udev/modprobe/systemd drop-ins when they changed, and
  warns when `bind = "auto"` or when the existing config no longer loads.
- On first install, generates the portal admin password, prints it, and keeps a copy
  in `/etc/water-bacnet/initial-password` (root only). Adds `water-bacnet` to group
  `systemd-journal` so the portal can show the service log. No firewall ports are
  opened or changed.

`sudo ./install.sh --uninstall` removes the units (service, portal, helper), the venv,
the code and its drop-ins. It keeps `/etc/water-bacnet` (config, portal password) and
`/var/lib/water-bacnet` (state).

Logs: `journalctl -u water-bacnet -f`. Every fresh/stale transition, read error and
rtl_433 restart is logged.

## Finding your meter id

`[meter] id` is the meter's **on-air Badger ORION id**. It has no default: until it is
set, `python -m water_bacnet --check` and the service exit with a message saying how
to find it, and the web portal shows a "Setup needed" banner.

The id is the **low 24 bits of the serial number stamped on the ORION endpoint**:

```sh
python3 -c 'print(24431537 & 0xFFFFFF)'   # stamped serial 24431537 -> id 7654321
```

Confirm it on the air near the meter (the service must not be running, since it owns
the dongle; `sudo systemctl stop water-bacnet` first):

```sh
rtl_433 -f 916.45M -s 1600k -R 223 -F json -M level   # -R 219 on older rtl_433 builds
```

Look for `"model":"Badger-ORION"` packets whose `"id"` matches. Neighbours' meters show
up too, with other ids. Set it on the portal's Settings page, or in the config:

```toml
[meter]
id = 7654321
```

## Configuration

`/etc/water-bacnet/config.toml`. Every key except `[meter] id` is optional;
`config.example.toml` lists all of them with their defaults. Edit it on the [web portal](#web-configuration-portal), or by
hand: the service notices the file changed and restarts itself on it within a few
seconds (`sudo systemctl restart water-bacnet` also works).

If `config.toml` ever fails to load, the service logs an ERROR and runs on
`config.toml.last-good` (the last config confirmed working) until it is fixed, so a
typo never takes the meter off BACnet.

"Fails to load" includes values that would make the service misbehave: every number
must be finite (TOML allows `inf`/`nan`), `stale_after_s` 60–86400, `restart_if_silent_s`
≥ 300, `refresh_s` 1–300, `vendor_id` 0–65535, `cycle_delay_s` 1–60, and `bind` must
parse as `"auto"` or an IPv4 address (a bare address means /24). A bad `[health]` or
`[portal]` value only falls back to that key's default with a warning. The portal
applies stricter checks still (for example the `[recovery]` formats the root helper
accepts) before it saves anything.

| Key | Default | Notes |
|---|---|---|
| `[meter] id` | none (`0` = not set) | **required**: on-air ORION id = stamped endpoint serial & 0xFFFFFF (see above) |
| `[meter] stale_after_s` | `900` | feed counts as stale after this long without a packet |
| `[source] mode` | `"rtl_433"` | `"rtl_433"` (SDR on this Pi) or `"state_file"` (follow a collector's state.json) |
| `[source] device` | `""` | rtl_433 `-d`: `""` = first dongle. Recommended: pin it by USB serial, e.g. `":00000001"` (set a unique one with `rtl_eeprom -s`); the recovery helper uses the serial to find the dongle |
| `[source] frequency` / `protocol` | `"916.45M"` / `223` | the Badger ORION decoder is 223 in rtl_433 25.02 and 219 in older builds; `install.sh` prints what the installed build calls it |
| `[bacnet] bind` | `"auto"` | `"auto"` = IPv4 address/prefix of the default-route interface, or explicit, e.g. `"192.0.2.10/24"`. Use an explicit address on a fixed (DHCP-reserved) IP. |
| `[bacnet] port` | `47808` | |
| `[bacnet] device_instance` | `2801` | must be unique on your BACnet internetwork |
| `[bacnet] device_name` / `description` / `location` | `"WaterMeter"` / generic / `""` | Device object properties |
| `[portal] listen` / `port` | `"0.0.0.0"` / `8080` | web portal address; env `WATER_BACNET_PORTAL_LISTEN` / `_PORT` override |
| `[portal] public_status` | `true` | status page and `/api/status` readable without logging in (the service log is always login-only) |
| `[storage] raw_log` | `true` | raw packets from our meter in `raw/YYYY-MM-DD.jsonl` (~60 MB/yr) |
| `[storage] raw_retention_days` | `365` | raw files older than this are deleted daily |
| `[storage] save_interval_s` | `900` | `state.json` is rewritten at most this often (SD wear), and on stop |
| `[recovery] usb_reset` / `power_cycle` / `reboot` | `true` / `true` / `false` | which recovery steps may run (rtl_433 mode) |
| `[recovery] uhubctl_path` / `hub_location` / `hub_port` | `"uhubctl"` / `"1-1"` / `""` | Pi 4: hub 1-1, all four ports switch together (`""` = all) |
| `[recovery] cycle_delay_s` | `5` | seconds of VBUS off during a power cycle (1–60) |
| `[recovery] usb_vid_pid` | `"0bda:2838"` | the dongle's USB id |
| `[recovery] max_power_cycles_per_day` / `max_reboots_per_week` | `4` / `1` | enforced by the root helper from its own ledger |
| `[recovery] step_timeout_s` | `120` | how long the service waits for the helper's answer |
| `[health] cpu_temp` / `thermal_path` | `true` / `"/sys/class/thermal/thermal_zone0/temp"` | AI 12 |
| `[health] throttled` / `vcgencmd_path` | `true` / `"vcgencmd"` | AI 13, BI 2 (`vcgencmd get_throttled`) |
| `[health] enclosure_sensor` | `"auto"` | `auto` (SHT31 at 0x44, then BME280 at 0x76/0x77), `sht31`, `bme280`, `none`; AI 14/15 |
| `[health] i2c_bus` / `i2c_address` | `1` / `0` | `/dev/i2c-1`; `0` = probe, or force `0x44`/`0x45`/`0x76`/`0x77` |
| `[health] poll_s` | `60` | health poll interval (minimum 5) |

With an explicit `bind`, the service waits up to 60 s at boot for that address to
appear before starting. If it never does, the start fails and systemd retries every 10 s.
The prefix matters: the service answers broadcast Who-Is on that subnet's broadcast
address, and also on 255.255.255.255.

## BACnet objects

Device **2801** `WaterMeter` (both configurable), vendor id 999 (unregistered), model
`Badger ORION via rtl_433`. All present-values are read-only. A WriteProperty
gets `write-access-denied`.

| Object | Name | Value | Units |
|---|---|---|---|
| analogInput 1 | Meter Register | register reading | cubicFeet |
| analogInput 2 | Meter Register (gallons) | register × 7.480519 | usGallons |
| analogInput 3 | Flow Rate | average over ~6 h of register history | cubicFeetPerHour |
| analogInput 4 | Flow Rate (recent) | average over the last ~1 h | cubicFeetPerHour |
| analogInput 5 | Seconds Since Packet | live age of the last packet | seconds |
| analogInput 6 | Signal SNR | from rtl_433 | decibels |
| analogInput 7 | Signal RSSI | from rtl_433 (relative dB) | decibels |
| analogInput 8 | Packets Per Hour | rolling 1 h | perHour |
| analogInput 9 | Collector rtl_433 Restarts | count | noUnits |
| analogInput 10 | USB Resets | recovery ladder: USB resets of the dongle (attempts) | noUnits |
| analogInput 11 | USB Power Cycles | recovery ladder: USB power cycles (attempts) | noUnits |
| analogInput 12 | CPU Temperature | SoC temperature | degreesCelsius |
| analogInput 13 | Throttled Flags (raw) | `vcgencmd get_throttled` as a number (e.g. 0x50005 = 327685) | noUnits |
| analogInput 14 | Enclosure Temperature | SHT31 / BME280 on I²C | degreesCelsius |
| analogInput 15 | Enclosure Humidity | SHT31 / BME280 on I²C | percentRelativeHumidity |
| binaryInput 1 | Feed Stale | active = no fresh data | (activeText `STALE`, inactiveText `OK`) |
| binaryInput 2 | Throttled/Undervoltage | active = any of `get_throttled` bits 0–3 set **now** (under-voltage, frequency capped, throttled, soft temperature limit) | (activeText `THROTTLED`, inactiveText `OK`) |

Object numbers never change: new objects are only ever appended.

Fault handling: when there is no fresh data, every analog input keeps its last good
value but gets `statusFlags` fault = true and `reliability = communication-failure`, and
binaryInput 1 goes **active**. "No fresh data" means the last packet is older than
`stale_after_s`, or the state file is missing or unreadable or its `updated` time is too
old. Everything recovers on its own when fresh data returns. AI 5 keeps counting up
while the feed is down.

AI 1–9 follow the feed as above. AI 10–11 are counters of the box's own recovery
attempts; they stay valid (no fault) while the feed is stale, because that is when
they matter. AI 12–15 and BI 2 come from the health poller: a sensor that is missing,
disabled in `[health]` or failing reports `reliability = no-sensor` with fault = true
(the last value is kept), and is logged once, not every poll. A Pi with no enclosure
sensor therefore shows AI 14/15 as permanently no-sensor; don't alarm on their
reliability unless one is fitted. Bits 16–19 of AI 13 ("has happened since boot")
do not set BI 2.

### Units: it's cubic feet

The register is **cubic feet**, 1:1 with the meter dial, with 1 cu ft resolution.
rtl_433 calls the field `volume_gal`, but for the ORION meters this was built against it
is cubic feet. Check AI 1 against your meter's dial once. AI 2 is the
only object in gallons (1 cu ft = 7.480519 US gal). The flow rates are cubic feet per
hour (bacpypes3 0.0.110 has the `cubicFeetPerHour` unit, so no per-minute conversion
is needed).

## Web configuration portal

`water-bacnet-portal.service` serves a small web UI on **`http://<pi-address>:8080/`**.
It is a separate process from the BACnet service, so it keeps working while the BACnet
service is down or crash-looping on a bad config. Standard library only (it runs on the
system `python3`, not the venv, so a broken venv cannot take it down too); no internet
needed; works on a phone.

**It is plain HTTP.** This is a LAN appliance: the password and session cookie cross the
network unencrypted. Keep it on a trusted LAN or management VLAN; do not port-forward it.

- **Status** (`/`, refreshes every 15 s): register in cu ft and US gal, Feed OK/STALE,
  packet age, packets/hour, rtl_433 restarts, signal, whether `water-bacnet` is running,
  box health (CPU temperature, throttle flags, enclosure sensor), host IP, uptime, disk
  free, version. Viewable without logging in unless `[portal] public_status = false`.
  The last 50 lines of the service journal are shown **only when logged in** (they can
  contain addresses, paths and errors).
- **`GET /api/status`**: the same as JSON, for scripts and monitoring.
- **Settings** (login required): every config key. Values are checked server-side
  (the new file must load with `load_config` and pass stricter range/format checks)
  before anything is written.
- **Password** (login required).

What **Save and apply** does:

1. Copies the current `config.toml` to `config.toml.bak-<UTC time>` (the last 10 are
   kept; they also hold the comments the portal does not preserve).
2. Writes the new `config.toml` atomically.
3. The BACnet service sees the change, exits, and systemd starts it again on the new
   file (a BACnet client may miss one poll).
4. The portal waits for the service to report, through its heartbeat
   `/run/water-bacnet/main-status.json`, that it is serving BACnet on the new file
   and stays up for 15 s. "Serving" is an explicit signal: the service records it in
   the heartbeat (`"ready_via": "signal"`) at the moment its sockets are bound, the same
   moment it sends `READY=1` to systemd. (A heartbeat from an older service without
   that field is treated as serving after 80 s up.) The new file then becomes `config.toml.last-good`.
5. If the service fails, falls back to last-good, or is not healthy within 150 s, the
   portal **puts `config.toml.last-good` back automatically**, keeps your version as
   `config.toml.rejected`, and shows the error and the service log. A bad bind address
   is caught after about 70 s (the service waits 60 s for the address to appear).

If the BACnet service is deliberately stopped (`inactive`), the file is saved but not
tested. Changing the portal's own `listen`/`port` restarts the portal; if it cannot
listen there it falls back to `0.0.0.0:8080`.

Security: one admin password, stored as PBKDF2-SHA256 (260,000 iterations, random salt)
in `/etc/water-bacnet/portal.passwd` (0600). Sessions are random tokens in an HttpOnly,
SameSite=Strict cookie (2 h idle, 24 h maximum). Every form has a CSRF token.
Logins are rate-limited (5 failures per client, 20 overall, per 5 minutes).

**Forgot the password?** On the Pi:

```sh
sudo -u water-bacnet /opt/water-bacnet/venv/bin/python -m water_bacnet.portal --set-password
```

Logs: `journalctl -u water-bacnet-portal -f`.

## Remote recovery and the root helper

In `rtl_433` mode a silent dongle is escalated one step per silent period
(`restart_if_silent_s`): restart rtl_433 → USB reset → USB power cycle (`uhubctl`, a
remote unplug) → reboot (only if `[recovery] reboot = true`). Any packet from the meter
resets the ladder. AI 10/11 count the attempts. [docs/HARDWARE.md](docs/HARDWARE.md)
has the reasoning and [docs/DESIGN.md](docs/DESIGN.md#recovery-ladder) the exact rules.

The BACnet service stays unprivileged. The privileged steps go through a small root
helper, and the interface between them is deliberately narrow:

- The service writes `/var/lib/water-bacnet/helper/request.json` (directory owned by
  `water-bacnet`, 0750) containing **only an action name** (`usb_reset`,
  `power_cycle` or `reboot`), an id and a timestamp.
- `water-bacnet-helper.path` starts `water-bacnet-helper.service` (root, oneshot),
  which checks the action against a whitelist and takes **every parameter** (USB
  id, uhubctl path, hub location/port, delay, whether reboot is allowed) from the
  root-owned `/etc/water-bacnet/config.toml`, never from the request. A compromised
  service can therefore ask for one of three fixed actions, but cannot make root run
  an arbitrary command or argument.
- The helper enforces its **own** rate limits from its own ledger,
  `/var/lib/water-bacnet-helper/ledger.json` (root only): `max_power_cycles_per_day`,
  `max_reboots_per_week`, and at most one USB reset per 10 minutes. The service's
  counters are not trusted for this.
- It answers in `response.json`, removes the request, and logs every action to the
  journal (`journalctl -u water-bacnet-helper`).

## Service hardening and liveness

`water-bacnet.service` runs as `water-bacnet` with no capabilities
(`CapabilityBoundingSet=` empty, `NoNewPrivileges`), `ProtectSystem=strict` (the only
writable paths are `/var/lib/water-bacnet` and `/run/water-bacnet`), `PrivateTmp`,
kernel/cgroup/clock protections, `DevicePolicy=closed` with only USB, I²C and the
VideoCore mailbox (`vcgencmd`) allowed, and `MemoryMax=256M`.

- `Type=notify`: the service sends `READY=1` once BACnet is bound, so `active`
  means serving. `TimeoutStartSec=180` covers the venv check, the 60 s bind wait and
  the socket check.
- `WatchdogSec=120`: `WATCHDOG=1` is sent from the BACnet refresh loop, and only while
  the meter source's own loop has completed an iteration in the last 60 s. A hung event
  loop, refresh loop or source loop stops the pings and systemd kills and restarts the
  service.
- The bind address is re-checked every 60 s. If the address is no longer on this host,
  or `bind = "auto"` now resolves to a different address, for more than 2 minutes, the
  service exits and systemd restarts it on the current address.
- `Restart=always`, `RestartSec=10`, `StartLimitIntervalSec=0`: it never gives up.
- `After=`/`Wants=time-sync.target network-online.target`. Not
  `systemd-time-wait-sync`, which would hold BACnet back until NTP answers.
- `ExecStartPre=+…/venv-check.sh`: the one root step (the `+`). If a Debian upgrade
  replaced Python (pyvenv.cfg's major.minor differs from `python3`) or `import
  bacpypes3` fails, it rebuilds the venv offline from `/opt/water-bacnet/vendor`
  (hash-checked). `/opt` is root-owned and read-only to the service on purpose, so the
  service itself could not repair it, and giving it write access to its own code would
  let a compromised service persist. When the venv is fine, the check costs about a
  second.

## Logs and SD-card wear

The installer makes the journal **persistent** (`Storage=persistent`,
`SystemMaxUse=64M`, `SystemMaxFileSize=8M`, `MaxRetentionSec=6month`). On an unattended
box, the logs you need most are the ones from just before a power cut, a watchdog
reboot or a crash; `journalctl -b -1` can only show them if they were on disk. Volatile
storage would lose exactly those. The cap bounds the space and the wear: the service
logs only on state changes, so 64 MB is many months of history. If `rsyslog` is
installed it writes everything a second time to `/var/log`; the installer warns, and
`apt purge rsyslog` removes the duplicate.

Also mount the root filesystem with **`noatime`** (the installer does not edit
`/etc/fstab`): in `/etc/fstab` change the root (`/`) line's options from `defaults` to
`defaults,noatime`, then reboot. Without it every file read is a write.

Other writes: `state.json` at most every `save_interval_s` (15 min) and on stop; the
raw packet log (~60 MB/year, pruned after `raw_retention_days`); the portal heartbeat
lives on tmpfs (`/run`).

## Updates

`sudo ./install.sh --unattended-upgrades` installs `unattended-upgrades` with an
`Origins-Pattern` of **Debian-Security only** and `Automatic-Reboot "false"`.
Raspberry Pi OS has no separate security suite to add (archive.raspberrypi.com
publishes no `<codename>-security`; checked 2026-10-18), so kernel and firmware updates
from the Raspberry Pi archive are NOT applied automatically. On **armhf** (32-bit)
Raspberry Pi OS the base is Raspbian, which has no Debian-Security origin, so this
setting installs nothing there: use the 64-bit OS. The pinned bacpypes3 in the venv
is never touched by apt; if a Python upgrade breaks the venv, the service rebuilds it
at its next start (above).

## Adding it to Mango Automation (or any BACnet client)

Any BACnet/IP client that can do Who-Is and ReadProperty / ReadPropertyMultiple works.
In Mango Automation:

1. Data Sources → add a **BACnet IP** data source, with its own local device instance
   (any instance not used elsewhere on your network) on port 47808.
2. Run a device discovery (it sends Who-Is). The meter should answer as **remote device
   2801** at the Pi's IP. Broadcasts do not cross routers or VLANs, so if the Pi is on
   another subnet enter the device by IP directly.
3. Set the update period to **1 minute**. Data only changes every few minutes, since the
   meter transmits about 22 times an hour.
4. Add points by object type and instance, with property `present-value`. For example,
   object type `ANALOG_INPUT` instance 1 is the meter register, and `BINARY_INPUT`
   instance 1 is Feed Stale. Mango spells object types in upper case with underscores, so
   a multi-state value appears as `MULTISTATE_VALUE` (this device has none).

Other clients (Niagara, Node-RED BACnet nodes, bacpypes/BACnet4J scripts, a BMS head
end) work the same way: discover or add device 2801 by IP and poll the objects above.

**Alarm on binaryInput 1 "Feed Stale" being active.** Also alarm on **binaryInput 2
"Throttled/Undervoltage"** (power supply or heat problem) and on **analogInput 15
"Enclosure Humidity" above ~70 %** (the seal is failing), if a sensor is fitted.
A rising AI 10/11 means the dongle needed recovery.

For Feed Stale: do NOT alarm on a flat meter
register. The register staying the same for hours is normal when little water is used,
and gaps of several minutes between packets are normal too. Only packet age shows
whether the feed is alive, and that is what Feed Stale reports. Alarming on the client's
own "point unreliable" state (from the fault/reliability flags) is fine as a second check.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install bacpypes3==0.0.110 pytest
.venv/bin/python -m pytest tests -q -p no:cacheprovider
# the example has no meter id, so --check on it explains how to find one and exits 2;
# give a scratch copy any id to try it:
mkdir -p /tmp/wb && sed 's/^id = 0$/id = 7654321/' config.example.toml > /tmp/wb/config.toml
.venv/bin/python -m water_bacnet --config /tmp/wb/config.toml --check
# portal against the scratch copy:
.venv/bin/python -m water_bacnet.portal --config /tmp/wb/config.toml --set-password
.venv/bin/python -m water_bacnet.portal --config /tmp/wb/config.toml --port 8080
```
