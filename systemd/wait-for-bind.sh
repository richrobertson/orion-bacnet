#!/bin/sh
# ExecStartPre helper for water-bacnet.service.
#
# Waits up to TIMEOUT seconds for the BACnet bind address to exist. Bridges and VLAN
# interfaces can come up after network-online.target, and a daemon bound to an
# address that does not exist yet dies at boot.
#   bind = "a.b.c.d/nn"  -> wait for a.b.c.d to be assigned; fail (exit 1) on timeout
#                           so systemd's Restart=on-failure retries.
#   bind = "auto"        -> wait for a default route; on timeout warn and carry on
#                           (the service then reports its own error).
# Usage: wait-for-bind.sh [CONFIG] [TIMEOUT]
CONFIG=${1:-/etc/water-bacnet/config.toml}
TIMEOUT=${2:-60}
# System python3: only stdlib tomllib is needed, so this works even with a broken venv.
PY=${WATER_BACNET_PYTHON:-/usr/bin/python3}

bind=$("$PY" -c 'import sys, tomllib
with open(sys.argv[1], "rb") as f:
    d = tomllib.load(f)
print(str(d.get("bacnet", {}).get("bind", "auto")).strip())' "$CONFIG" 2>/dev/null) || bind=auto
[ -n "$bind" ] || bind=auto
ip=${bind%%/*}

i=0
while [ "$i" -lt "$TIMEOUT" ]; do
    if [ "$bind" = "auto" ]; then
        if [ -n "$(ip -4 route show default 2>/dev/null)" ]; then exit 0; fi
    else
        if ip -4 -o addr show 2>/dev/null | grep -qF "inet $ip/"; then exit 0; fi
    fi
    if [ "$i" -eq 0 ]; then
        if [ "$bind" = "auto" ]; then
            echo "water-bacnet: waiting up to ${TIMEOUT}s for a default route (bind=auto)" >&2
        else
            echo "water-bacnet: waiting up to ${TIMEOUT}s for bind address $ip" >&2
        fi
    fi
    sleep 1
    i=$((i + 1))
done

if [ "$bind" = "auto" ]; then
    echo "water-bacnet: no default route after ${TIMEOUT}s; starting anyway" >&2
    exit 0
fi
echo "water-bacnet: bind address $ip not present after ${TIMEOUT}s" >&2
exit 1
