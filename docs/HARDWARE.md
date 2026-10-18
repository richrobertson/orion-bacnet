# Outdoor PoE receiver: hardware design

A weatherproof, PoE-powered Raspberry Pi + RTL-SDR, mounted outside near the meter,
running orion-bacnet in `rtl_433` mode. The goals: it runs for years without a site
visit, and anything that can go wrong can be fixed remotely.

Nothing here is required. The software runs on any Raspberry Pi (or any Debian box)
with an RTL-SDR. This is the build the software's recovery and health features were
designed around.

## What drives the design

These come from a proof-of-concept receiver run indoors on a general-purpose server.

- **The radio link is usually easy.** A Badger ORION CE endpoint transmits every few
  seconds, hopping across 902–928 MHz. With the endpoint out of its pit a cheap dongle
  saw RSSI around −5 dB and SNR around 20 dB, so no directional antenna is needed. A
  meter pit (concrete, a steel or cast-iron lid, often water) is the main attenuator,
  and getting the receiver close to it, outdoors, is what fixes that.
- **About 22 packets/hour is the normal rate.** At 1.6 Msps rtl_433 hears about 1.6 of
  the 26 MHz the endpoint hops across, so it catches only a fraction of transmissions.
  Gaps of several minutes are normal; hopping the receiver frequency does not help.
- **The SDR is the weakest part.** RTL-SDR dongles wedge. A USB bus reset cleared one
  wedge; another needed a physical unplug. The design has to be able to cut the
  dongle's power remotely.
- **SD-card wear and condensation** are what kill outdoor Pis over several years. Heat
  and cold mostly do not.

## Recommended build

| Part | Choice | Why |
|---|---|---|
| Computer | **Raspberry Pi 4 Model B, 2 GB** | rtl_433 at 1.6 Msps uses a fraction of one core. On the Pi 4, `uhubctl` can **switch USB port power** (all four ports switch together), which gives a remote replug. Long production commitment. A Pi 5 runs hotter, draws more power and adds nothing here. |
| PoE | **802.3af/at gigabit PoE splitter → USB-C 5 V, ≥2.5 A (5.1–5.2 V output)**, mounted beside the Pi | Fanless and separately replaceable. The official PoE+ HAT has a fan, and a fan is the first thing to die outdoors. Look for 5.1 V or more out, because the Pi flags undervoltage at about 4.63 V. |
| Cooling | Fanless aluminium heatsink case (the "armour" style) | No moving parts. The case conducts heat to the air inside the enclosure. |
| Storage | **Industrial pSLC microSD, 16–32 GB, ×2** (SanDisk Industrial, Kingston Industrial, Swissbit) | Consumer cards are the number-one cause of Pis dying after a few years. Clone the second card and tape it inside the lid as a field spare. |
| SDR | RTL-SDR with a **TCXO**, ideally the same model you validated in the site survey | The TCXO keeps 916.45 MHz on frequency from cold mornings to sun-heated afternoons. Give it a unique serial (`rtl_eeprom -s`) and pin it in `[source] device = ":<serial>"`. ⚠️ An RTL-SDR Blog **V4** needs librtlsdr ≥ 2.0.2. Use Pi OS **Trixie**, or test with Bookworm's library before you commit. |
| SDR mounting | Plug it into a **USB 2.0 port** (black) with a short 15–30 cm USB extension | Keeps the tuner away from the Pi's own RF noise. |
| Antenna | **915 MHz ¼-wave or dipole, SMA, inside the enclosure**, mounted vertically | The signal is usually strong and a plastic box is RF-transparent. Keeping the antenna inside means one less hole, no water path and no lightning path. Leave a blanked hole position for an N-type bulkhead in case an external antenna is ever needed. |
| RF filter (optional) | 902–928 MHz SAW bandpass, SMA inline | Cheap insurance against nearby 850 MHz cellular desensing the dongle. Leave it out unless SNR comes in below ~10 dB. |
| Environment sensor | **SHT31 or BME280** on I²C | Enclosure temperature and humidity. **Rising humidity warns you the seal is failing** months before anything breaks. Exported as BACnet points (see below). |
| Enclosure | **Polycarbonate, IP66/67, UV-stabilised, light grey, ~200×150×100 mm**, with a removable backplate | It must not be metal, which would block RF. A light colour reduces solar gain. The backplate lets you assemble everything on the bench. |
| Venting | **ePTFE breather vent plug (M12)** at the bottom | 🔑 A sealed box "breathes" with every temperature swing and pulls in moist air that cannot get back out. A breather equalises pressure without letting water in. This is the single most important longevity part. |
| Desiccant | Rechargeable silica packs (the colour-change kind) | Replace or recharge them at the yearly check. |
| Cable entry | IP68 RJ45 bulkhead, or a **split-insert cable gland**. Enter from the **bottom only**, with a drip loop. | Never enter through the top or sides. |
| Cable | Outdoor-rated (UV, and gel-filled if any of it is buried) **shielded Cat6** with shielded RJ45s | A sun-rated jacket lasts; indoor PVC cracks within a few years. |
| Surge | **PoE-rated Ethernet surge protector at the building entry**, bonded to building ground | It protects the switch port as much as the Pi. Do not also ground the shield at the Pi end. |
| Mounting | The building wall nearest the meter, under an eave, on the **shadiest face** (north or east in the northern hemisphere), 2–3 m up, with line of sight to the meter. Stainless hardware. | Shade matters more than anything else for summer temperatures. A small sun shield helps if there is no eave. |

Rough cost: **$250–300** (2026 prices), including the spare card, surge protector and
cable.

### Power budget

Pi 4 running rtl_433: ~3.5–4 W. Dongle: ~1.5 W. Sensor: negligible. That is **~5.5 W at
5 V**, or about 7 W at the switch after splitter losses. Plain **802.3af** is enough;
802.3at is not needed.

### Thermal

The Pi gives off about 5 W of heat, which keeps the enclosure a few degrees above the
outside air. That helps keep it above the dew point in winter. In summer, shade is what
keeps the SoC well below its ~80 °C throttle point. Check your local ambient range
against the parts' ratings, and watch the throttle flags (below) through the first
summer.

## Layout (on the backplate)

```
 ┌──────────────────────────────────────────┐
 │  [915 MHz antenna, vertical]   ▲ top     │
 │                                           │
 │  ┌────────────┐   ┌──────────────┐        │
 │  │ RTL-SDR    │←──│ Pi 4 in       │        │
 │  │ (short USB │   │ heatsink case │        │
 │  │  ext.)     │   └──────┬───────┘        │
 │  └────────────┘          │ USB-C 5 V      │
 │  [SHT31]          ┌──────┴───────┐        │
 │  [desiccant]      │ PoE splitter │        │
 │                   └──────┬───────┘        │
 │                          │ RJ45           │
 └──────────[vent]──────[gland]──────────────┘
                             │ drip loop → surge protector → switch
```

## Network

- **DHCP reservation** (or a static address). Set an explicit
  `bind = "a.b.c.d/nn"` in the config; the service waits up to 60 s for that address
  at boot.
- 🔑 **Put it on its own isolated VLAN, not your management network.** An outdoor RJ45
  is a port anyone can reach, and unplugging the Pi must not hand them your management
  LAN. A small BMS/IoT VLAN is ideal. Then allow your BACnet client → Pi UDP 47808 and
  management SSH from the admin side through your firewall. Broadcast Who-Is does not
  cross VLANs, so **add the device in the BACnet client by IP** (the README covers this).
- NTP from your router or another local server. The Pi has no RTC, so flow maths before
  the first time sync would be wrong; the software uses the monotonic clock for ages
  and rates so this cannot freeze or fake the feed.

## Remote recovery ladder (no site visit at any step)

| Step | Trigger | Action | Status |
|---|---|---|---|
| 1 | No packet for `restart_if_silent_s` (30 min) | Kill and restart rtl_433 | ✅ in `sources.py` |
| 2 | Still silent after the restart | `USBDEVFS_RESET` on the dongle | ✅ code (root helper); needs on-hardware verification |
| 3 | Still silent | **`uhubctl -l 1-1 -a cycle`**: a real VBUS power cut, i.e. a remote unplug | ✅ code (root helper); needs on-hardware verification |
| 3b | Still silent (opt-in, `[recovery] reboot = true`) | Reboot the Pi | ✅ code, off by default |
| 4 | OS or kernel hang | BCM2835 **hardware watchdog** reboots the Pi | ✅ `install.sh` drop-in `RuntimeWatchdogSec=15s` (if `/dev/watchdog` exists); needs on-hardware verification |
| 5 | Pi unreachable | **Power-cycle the PoE port** from the switch or its controller | depends on your switch |
| 6 | Card corrupted | Swap in the spare card from the lid | site visit, ~5 min |

Steps 2 and 3 matter. In the proof of concept, restarting rtl_433 (step 1) did **not**
clear a wedged dongle. One wedge needed `USBDEVFS_RESET` and another needed a physical
replug, which is what step 3 does remotely.

## What the software does for this hardware

"Code" means written and unit-tested off-target; see the README's
[Status](../README.md#status) section for what still has to be verified on a real Pi.

1. **Recovery escalation (steps 2–3 above).** `Rtl433Source` runs the ladder (restart
   → USB reset → power cycle → optional reboot, one step per silent period, reset by
   any packet). The privileged steps go through a root oneshot
   (`water-bacnet-helper.path` → `.service`) triggered by a request file that carries
   only the action name; the helper reads its parameters from the root-owned config
   and enforces its own rate limits. `install.sh` installs the units, the request
   directory and `uhubctl`. **On the Pi:** confirm `uhubctl -l 1-1 -a cycle` really
   drops VBUS to the dongle on your Pi 4 revision, that `USBDEVFS_RESET` clears a
   wedge, and that the dongle comes back with the same serial.
2. **Hardware watchdog.** `install.sh` writes
   `/etc/systemd/system.conf.d/water-bacnet-watchdog.conf` (`RuntimeWatchdogSec=15s`,
   `RebootWatchdogSec=2min`) when `/dev/watchdog` exists, then `daemon-reexec`. The
   service itself is `Type=notify` with `WatchdogSec=120`, pinged only while the source
   loop is alive. **On the Pi:** `echo c > /proc/sysrq-trigger` must reboot it;
   `systemctl show -p WatchdogUSec water-bacnet` = 2 min; `kill -STOP` the service and
   watch systemd restart it.
3. **SD wear.** The journal is persistent but capped (64 MB, 6 months; the README's
   "Logs and SD-card wear" explains why persistent beats volatile here). `raw/*.jsonl`
   is pruned after `raw_retention_days` (365); `state.json` is written at most every
   `save_interval_s`. `noatime` is documented, not scripted: edit `/etc/fstab` by hand.
   **On the Pi:** `journalctl --disk-usage`, `journalctl -b -1` after a reboot, and
   whether `rsyslog` is installed (purge it).
4. **Health points over BACnet** (`water_bacnet/health.py`), numbered after the
   recovery counters AI10/AI11:
   - AI12 CPU temperature °C (`/sys/class/thermal/thermal_zone0/temp`)
   - AI13 throttle flags, raw (`vcgencmd get_throttled`)
   - AI14 enclosure temperature °C
   - AI15 enclosure RH %
   - BI2 throttled or under-voltage now (`get_throttled` bits 0–3)

   The enclosure sensor is found automatically (SHT31 at 0x44, then BME280 at
   0x76/0x77) on `/dev/i2c-1`; a missing sensor reports reliability no-sensor. Alarm
   on BI1 Feed Stale, BI2, and AI15 > ~70 %. **On the Pi:** enable I²C
   (`dtparam=i2c_arm=on`), check the sensor is found (`journalctl -u water-bacnet |
   grep health`), that `vcgencmd` works as the `water-bacnet` user under the unit's
   `DeviceAllow` (it needs `/dev/vchiq`; group `video`), and compare the readings with
   a reference thermometer/hygrometer. The BME280 humidity maths is checked against
   the datasheet's floating-point formula only, not a real part.
5. **Updates.** `install.sh --unattended-upgrades`: Debian-Security only, automatic
   reboot off (Raspberry Pi OS has no separate security suite; on armhf this matches
   nothing, so use arm64). The venv is rebuilt offline from the vendored bacpypes3
   wheel by an `ExecStartPre=+` check if a Python upgrade breaks it. **On the Pi:**
   `unattended-upgrade --dry-run -d` lists only Debian-Security origins.
6. **Time.** The unit is `After=`/`Wants=time-sync.target` (not
   `systemd-time-wait-sync`, which would block BACnet while NTP is unreachable), and
   ages, silence and rates use the monotonic clock, so a wall-clock jump at the first
   NTP sync cannot freeze or fake the feed.

## Before you build: 1-hour site survey

1. **Leave the ORION endpoint where it will live** (back in the pit, lid closed),
   because that is the worst case the receiver has to handle.
2. Find your meter's id: the low 24 bits of the serial stamped on the endpoint,
   `serial & 0xFFFFFF` (for example serial 24431537 → id 7654321).
3. Stand at the planned mount spot with a laptop, the same dongle model and the
   indoor antenna, and run for one hour:
   `rtl_433 -f 916.45M -s 1600k -R 223 -F json -M level`
   (protocol 219 on rtl_433 builds older than 25.02; check `rtl_433 -R help`).
4. Pass mark: **~20 packets/hour from your meter's id** with SNR ≥ 10 dB. Neighbours'
   meters will show up too; ignore any other id.
   - Fewer than that: try a higher spot or one closer to the meter.
   - Only as a last resort: an external antenna.

## Commissioning checklist

- [ ] On the bench: `install.sh`, `[meter] id` set, `--check`, packets decoding,
      `uhubctl` cycles the dongle, the watchdog reboots on
      `echo c > /proc/sysrq-trigger`, AI12–15/BI2 read sensible values, the root mount
      in `/etc/fstab` has `noatime`
- [ ] Burn-in: 48 h on the bench at ~22 packets/hour
- [ ] Clone the card to the spare and tape it in the lid
- [ ] Mount, seal, fit the vent, fresh desiccant, drip loop, surge protector bonded
- [ ] Switch port on its own VLAN, DHCP reservation, firewall rule for the BACnet client
- [ ] Inventory/monitoring entries (ping, plus the portal's `/api/status`); BACnet
      client: device by IP, alarms on BI1/BI2/RH
- [ ] If it replaces an existing reader, run both for a week and compare registers
      against the meter's dial before retiring the old one
- [ ] Yearly: look at the RH trend, recharge the desiccant
