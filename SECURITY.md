# Security policy

## Supported versions

Only the latest release (and `main`) receives security fixes. Upgrade by pulling the
new release and re-running `sudo ./install.sh`.

| Version | Supported |
|---|---|
| latest release / `main` | yes |
| anything older | no |

## Reporting a vulnerability

Please report privately through GitHub's
[private vulnerability reporting](https://github.com/richrobertson/orion-bacnet/security/advisories/new)
(Security tab, "Report a vulnerability"). Do not open a public issue.

Include the version or commit, what you did, and what happened. You should get an
answer within a week. This is a one-person project, so fixes are best-effort.

## Threat model and scope

orion-bacnet is a **LAN-only appliance** for a Raspberry Pi. It is designed to sit on a
trusted building-management or management VLAN, never on the internet.

In scope:

- **The root helper** (`water-bacnet-helper.service` / `.path`). The unprivileged
  service drops a request file that triggers a root oneshot which may reset the USB
  dongle, power-cycle USB ports or reboot the Pi. Anything that lets the
  `water-bacnet` user, or a network client, make the helper do more than those
  rate-limited actions, take parameters from the request file, or write outside its
  own state directory is a vulnerability.
- **The web portal** (`water-bacnet-portal.service`, TCP 8080). It is **plain HTTP by
  design**: the password and session cookie cross the LAN unencrypted, and someone who
  can sniff that LAN can take over a session. That is accepted and documented, not a
  bug. Bypassing authentication, CSRF protection, the login rate limit or the
  server-side config validation, getting code execution, or reading/writing files
  outside `/etc/water-bacnet` and `/var/lib/water-bacnet` *is* in scope.
- **The BACnet/IP server** (UDP 47808). BACnet/IP has no authentication; every object
  is read-only. Any way to change a value, crash the service, or use it as a traffic
  amplifier is in scope.
- **`install.sh`** and the systemd hardening in the units.
- Parsing of `rtl_433` output and of a collector's `state.json`.

Out of scope:

- Attacks that need the portal or BACnet port exposed to the internet, or that need
  root, physical access, or the ability to sniff the LAN (see above).
- Spoofed ORION radio packets. The meter's radio protocol is unauthenticated; anyone
  in range can transmit a fake reading. Readings are for monitoring, not billing.
- Vulnerabilities in rtl_433, bacpypes3 or the OS itself (report those upstream),
  unless this project uses them in an unsafe way.
