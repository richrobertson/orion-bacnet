#!/usr/bin/env bash
# orion-bacnet (water-bacnet) installer for Raspberry Pi OS Bookworm / Trixie (arm64 or armhf).
#
#   sudo ./install.sh                        install or upgrade (idempotent)
#   sudo ./install.sh --with-rtl433          also install rtl-433 even if mode is state_file
#   sudo ./install.sh --unattended-upgrades  also set up security-only automatic updates
#   sudo ./install.sh --no-system-tuning     leave the host alone (no hardware watchdog,
#                                            journald or apt python3-venv); automatic on Proxmox
#   sudo ./install.sh --uninstall            remove units, venv and code; KEEPS
#                                            /etc/water-bacnet and /var/lib/water-bacnet
#
# Also installs the web configuration portal (water-bacnet-portal.service, TCP 8080)
# and, on first install, prints its generated admin password; the root recovery helper
# (water-bacnet-helper.path/.service); the hardware watchdog (if /dev/watchdog exists)
# and a persistent, size-capped journal.
#
# apt is only used for python3-venv, (rtl_433 mode) rtl-433 and uhubctl, and
# (--unattended-upgrades) unattended-upgrades, all with --no-install-recommends.
# Nothing else is upgraded. bacpypes3 is installed OFFLINE from vendor/ (hash-checked).
set -euo pipefail

APP=water-bacnet
PREFIX=/opt/water-bacnet
VENV=$PREFIX/venv
ETC=/etc/water-bacnet
CONFIG=$ETC/config.toml
STATE=/var/lib/water-bacnet
UNIT=/etc/systemd/system/$APP.service
PORTAL_UNIT=/etc/systemd/system/$APP-portal.service
HELPER_SERVICE=/etc/systemd/system/$APP-helper.service
HELPER_PATH=/etc/systemd/system/$APP-helper.path
HELPER_DIR=$STATE/helper
PASSWD=$ETC/portal.passwd
INITIAL_PW=$ETC/initial-password
MODPROBE=/etc/modprobe.d/$APP-rtlsdr-blacklist.conf
UDEV=/etc/udev/rules.d/60-$APP-rtlsdr.rules
WATCHDOG_DROPIN=/etc/systemd/system.conf.d/$APP-watchdog.conf
JOURNALD_DROPIN=/etc/systemd/journald.conf.d/$APP.conf
UU_DROPIN=/etc/apt/apt.conf.d/52$APP-unattended-upgrades
UU_AUTO=/etc/apt/apt.conf.d/20$APP-auto-upgrades
BACPYPES3_PIN="bacpypes3==0.0.110"
SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

WITH_RTL433=0
UNINSTALL=0
UNATTENDED=0
SYSTEM_TUNING=1

log()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARNING: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

for arg in "$@"; do
    case "$arg" in
        --with-rtl433)         WITH_RTL433=1 ;;
        --unattended-upgrades) UNATTENDED=1 ;;
        --no-system-tuning)    SYSTEM_TUNING=0 ;;
        --uninstall)           UNINSTALL=1 ;;
        -h|--help)             usage 0 ;;
        *)                     warn "unknown argument: $arg"; usage 2 ;;
    esac
done

# On a shared host (a hypervisor, a box that already runs other services) the watchdog,
# journald and system packages are the host's business, not this package's. Proxmox runs its own
# watchdog-mux for HA fencing, so never add a second one there.
if [[ -d /etc/pve && $SYSTEM_TUNING -eq 1 ]]; then
    log "Proxmox host detected; skipping host-wide tuning (as with --no-system-tuning)"
    SYSTEM_TUNING=0
fi


[[ $EUID -eq 0 ]] || die "run as root: sudo $0 $*"
command -v systemctl >/dev/null || die "systemd is required"

pkg_installed() {
    dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}

APT_UPDATED=0
apt_install() {
    # install only what is missing; never upgrade anything else
    local missing=()
    local p
    for p in "$@"; do
        pkg_installed "$p" || missing+=("$p")
    done
    [[ ${#missing[@]} -eq 0 ]] && return 0
    if [[ $APT_UPDATED -eq 0 ]]; then
        log "apt-get update"
        apt-get update -q
        APT_UPDATED=1
    fi
    log "apt-get install --no-install-recommends ${missing[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${missing[@]}"
}

# Write a root-owned file only when its content changed, so an upgrade refreshes our
# drop-ins and an unchanged one is left alone. Returns 0 if it was (re)written.
install_content() {
    local dest=$1 mode=$2 content=$3
    if [[ -f $dest ]] && [[ "$(cat "$dest")" == "$content" ]]; then
        return 1
    fi
    install -d -m 0755 "$(dirname "$dest")"
    printf '%s\n' "$content" >"$dest.tmp"
    chmod "$mode" "$dest.tmp"
    mv -f "$dest.tmp" "$dest"
    return 0
}

# ---------------------------------------------------------------- uninstall
if [[ $UNINSTALL -eq 1 ]]; then
    log "Uninstalling $APP"
    for u in "$APP-helper.path" "$APP-helper.service" "$APP-portal.service" "$APP.service"; do
        if systemctl list-unit-files "$u" >/dev/null 2>&1; then
            systemctl disable --now "$u" 2>/dev/null || true
        fi
    done
    rm -f "$UNIT" "$PORTAL_UNIT" "$HELPER_SERVICE" "$HELPER_PATH"
    systemctl daemon-reload
    systemctl reset-failed "$APP.service" "$APP-portal.service" "$APP-helper.service" \
        "$APP-helper.path" 2>/dev/null || true
    rm -rf "$PREFIX"
    # our own drop-ins (not the config) -- re-enables the DVB driver after reboot
    rm -f "$MODPROBE" "$UDEV" "$UU_DROPIN" "$UU_AUTO"
    if command -v udevadm >/dev/null; then udevadm control --reload-rules || true; fi
    if [[ -f $WATCHDOG_DROPIN ]]; then
        rm -f "$WATCHDOG_DROPIN"
        systemctl daemon-reexec || true
    fi
    # The cap is removed; /var/log/journal (persistent storage) stays until deleted.
    if [[ -f $JOURNALD_DROPIN ]]; then
        rm -f "$JOURNALD_DROPIN"
        systemctl restart systemd-journald || true
    fi
    log "Removed units, venv, code and drop-ins. Kept: $ETC (config, portal password), $STATE (state), user '$APP'."
    log "Packages python3-venv / rtl-433 / uhubctl / unattended-upgrades were left installed."
    exit 0
fi

# ---------------------------------------------------------------- checks
# shellcheck source=/dev/null
. /etc/os-release 2>/dev/null || true
case "${VERSION_CODENAME:-}" in
    bookworm|trixie) ;;
    *) warn "tested on Raspberry Pi OS Bookworm/Trixie; this is '${PRETTY_NAME:-unknown}'" ;;
esac
ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
case "$ARCH" in
    arm64|armhf|amd64) ;;
    *) warn "untested architecture: $ARCH" ;;
esac

command -v python3 >/dev/null || die "python3 not found"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "Python 3.11+ required (found $(python3 -V 2>&1))"

[[ -f $SRC/water_bacnet/__init__.py ]] || die "run install.sh from the unpacked source tree (any directory name); $SRC has no water_bacnet/"
for f in "$APP.service" "$APP-portal.service" venv-check.sh wait-for-bind.sh; do
    [[ -f $SRC/systemd/$f ]] || die "missing $SRC/systemd/$f"
done
[[ -f $SRC/vendor/SHA256SUMS ]] || die "missing $SRC/vendor/SHA256SUMS (vendored bacpypes3 wheel)"

# Settings that change what we install: from the existing config if there is one,
# else the shipped example. read_key FILE SECTION KEY DEFAULT
read_key() {
    python3 - "$@" <<'PY' 2>/dev/null || echo "$4"
import sys, tomllib
path, section, key, default = sys.argv[1:5]
with open(path, "rb") as f:
    v = tomllib.load(f).get(section, {}).get(key, default)
print(str(v).lower() if isinstance(v, bool) else v)
PY
}
CFG_SRC=$CONFIG
[[ -f $CFG_SRC ]] || CFG_SRC=$SRC/config.example.toml
MODE=$(read_key "$CFG_SRC" source mode rtl_433)
BIND=$(read_key "$CFG_SRC" bacnet bind auto)
POWER_CYCLE=$(read_key "$CFG_SRC" recovery power_cycle true)
log "Source mode: $MODE"
NEED_RTL433=0
[[ $MODE == rtl_433 || $WITH_RTL433 -eq 1 ]] && NEED_RTL433=1

# ---------------------------------------------------------------- packages
if [[ $SYSTEM_TUNING -eq 1 ]]; then
    apt_install python3-venv
else
    # No apt on a shared host: python3-venv can pull a partial interpreter upgrade.
    # venv-check.sh builds the venv --without-pip from the vendored wheel instead.
    log "Not installing python3-venv (--no-system-tuning); building the venv without pip"
fi
if [[ $NEED_RTL433 -eq 1 ]]; then
    apt_install rtl-433
    # Recovery step 3 (remote replug of the dongle) needs uhubctl.
    if [[ $POWER_CYCLE == true ]]; then
        apt_install uhubctl || warn "could not install uhubctl; USB power-cycle recovery will fail"
    fi
fi

# ---------------------------------------------------------------- user
getent group plugdev >/dev/null || groupadd --system plugdev
if ! id -u "$APP" >/dev/null 2>&1; then
    log "Creating system user $APP"
    useradd --system --user-group --home-dir "$STATE" --no-create-home \
        --shell /usr/sbin/nologin --groups plugdev "$APP"
else
    id -nG "$APP" | tr ' ' '\n' | grep -qx plugdev || usermod -aG plugdev "$APP"
fi
# The web portal shows `journalctl -u water-bacnet`; reading the journal needs this group.
if getent group systemd-journal >/dev/null; then
    id -nG "$APP" | tr ' ' '\n' | grep -qx systemd-journal || usermod -aG systemd-journal "$APP"
else
    warn "group systemd-journal not found; the portal will not show the service log"
fi
# [health]: I2C enclosure sensor (/dev/i2c-*: group i2c) and vcgencmd (/dev/vchiq:
# group video). Raspberry Pi OS has both groups; elsewhere they are skipped.
for g in i2c video; do
    if getent group "$g" >/dev/null; then
        id -nG "$APP" | tr ' ' '\n' | grep -qx "$g" || usermod -aG "$g" "$APP"
    fi
done

# ---------------------------------------------------------------- venv
# bacpypes3 comes from the vendored wheel, checked against vendor/SHA256SUMS and
# installed with pip --no-index --require-hashes: no internet, now or when the venv has
# to be rebuilt later. venv-check.sh also runs as ExecStartPre=+ on every service start
# (self-heal after a Debian Python upgrade), so install and repair share one code path.
install -d -m 0755 "$PREFIX" "$PREFIX/libexec" "$PREFIX/vendor"
(cd "$SRC/vendor" && sha256sum -c --quiet SHA256SUMS) \
    || die "vendor/ does not match vendor/SHA256SUMS; refusing to install"
rm -f "$PREFIX"/vendor/*.whl
install -m 0644 "$SRC"/vendor/*.whl "$SRC/vendor/SHA256SUMS" "$PREFIX/vendor/"
install -m 0755 "$SRC/systemd/venv-check.sh" "$PREFIX/libexec/venv-check.sh"
log "Checking the venv ($BACPYPES3_PIN, offline from $PREFIX/vendor)"
"$PREFIX/libexec/venv-check.sh" "$PREFIX" || die "could not build the venv $VENV (see above)"

# ---------------------------------------------------------------- code
log "Installing code to $PREFIX"
rm -rf "$PREFIX/water_bacnet.new"
cp -R "$SRC/water_bacnet" "$PREFIX/water_bacnet.new"
find "$PREFIX/water_bacnet.new" -name '__pycache__' -type d -prune -exec rm -rf {} +
rm -rf "$PREFIX/water_bacnet"
mv "$PREFIX/water_bacnet.new" "$PREFIX/water_bacnet"
"$VENV/bin/python" -m compileall -q "$PREFIX/water_bacnet" >/dev/null || true
install -m 0755 "$SRC/systemd/wait-for-bind.sh" "$PREFIX/libexec/wait-for-bind.sh"
for f in README.md docs/HARDWARE.md docs/DESIGN.md config.example.toml; do
    if [[ -f $SRC/$f ]]; then
        install -D -m 0644 "$SRC/$f" "$PREFIX/$f"
    fi
done
rm -f "$PREFIX/HARDWARE.md"  # moved to docs/ (older installs)
chown -R root:root "$PREFIX"

# ---------------------------------------------------------------- config
# Group-writable for the portal, which replaces config.toml atomically (rename) and
# keeps config.toml.last-good / .bak-* / portal.passwd here.
install -d -m 0770 -o root -g "$APP" "$ETC"
if [[ -f $CONFIG ]]; then
    log "Keeping existing $CONFIG"
    # A hand-made config is often root:root; the unprivileged service must read it.
    # (The portal's own rewrites are owned by $APP and already readable.)
    if [[ $(stat -c %U "$CONFIG") != "$APP" ]]; then
        chgrp "$APP" "$CONFIG"
        chmod g+r,o-rwx "$CONFIG"
    fi
else
    log "Installing $CONFIG (edit it, or use the web portal)"
    install -m 0640 -o root -g "$APP" "$SRC/config.example.toml" "$CONFIG"
fi
install -d -m 0750 -o "$APP" -g "$APP" "$STATE"
# last-good: what the service falls back to if config.toml ever fails to load, and
# what the portal rolls back to. Seed it from the current config if that loads.
if [[ ! -f $CONFIG.last-good ]]; then
    if runuser -u "$APP" -- env PYTHONPATH="$PREFIX" "$VENV/bin/python" -c \
        'import sys; from water_bacnet.config import load_config; load_config(sys.argv[1])' \
        "$CONFIG" 2>/dev/null; then
        install -m 0640 -o "$APP" -g "$APP" "$CONFIG" "$CONFIG.last-good"
        log "Saved $CONFIG.last-good"
    else
        warn "$CONFIG does not load; not seeding $CONFIG.last-good"
    fi
fi
# An upgrade can add validation that an old config fails; the service would then run
# on last-good. Say so now rather than leave it to the journal.
if ! runuser -u "$APP" -- env PYTHONPATH="$PREFIX" "$VENV/bin/python" -c \
    'import sys; from water_bacnet.config import load_config; load_config(sys.argv[1])' \
    "$CONFIG"; then
    warn "$CONFIG does not load with this version (see above); the service will use $CONFIG.last-good until it is fixed"
fi

# ---------------------------------------------------------------- portal password
NEW_PASSWORD=""
if [[ ! -s $PASSWD ]]; then
    NEW_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')
    printf '%s\n' "$NEW_PASSWORD" | runuser -u "$APP" -- env PYTHONPATH="$PREFIX" \
        "$VENV/bin/python" -m water_bacnet.portal --config "$CONFIG" \
        --set-password --password-stdin >/dev/null
    chmod 0600 "$PASSWD"
    (umask 077; printf '%s\n' "$NEW_PASSWORD" >"$INITIAL_PW")
    chown root:root "$INITIAL_PW"
    chmod 0600 "$INITIAL_PW"
    log "Generated the web portal admin password (also in $INITIAL_PW, root only)"
fi

# ---------------------------------------------------------------- RTL-SDR
REPLUG=0
if [[ $NEED_RTL433 -eq 1 ]]; then
    # Rewritten whenever the shipped content differs, so an upgrade refreshes them.
    if install_content "$MODPROBE" 0644 "# installed by water-bacnet: the RTL2832U must be used as an SDR, not a DVB-T tuner
blacklist dvb_usb_rtl28xxu"; then
        log "Blacklisted the DVB-T driver so rtl_433 can claim the dongle ($MODPROBE)"
        REPLUG=1
    fi
    if install_content "$UDEV" 0644 '# installed by water-bacnet: RTL2832U SDR dongles readable by plugdev
SUBSYSTEMS=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0660"
SUBSYSTEMS=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", GROUP="plugdev", MODE="0660"'; then
        log "Installed the udev rule giving RTL-SDR dongles to group plugdev ($UDEV)"
        udevadm control --reload-rules || true
        udevadm trigger --subsystem-match=usb --attr-match=idVendor=0bda || true
    fi
    if grep -q '^dvb_usb_rtl28xxu ' /proc/modules 2>/dev/null; then
        if modprobe -r dvb_usb_rtl28xxu 2>/dev/null; then
            log "Unloaded dvb_usb_rtl28xxu"
        else
            warn "dvb_usb_rtl28xxu is loaded and could not be unloaded"
        fi
        REPLUG=1
    fi
    if command -v rtl_433 >/dev/null; then
        log "rtl_433 version: $(rtl_433 -V 2>&1 | head -n1)"
        badger=$(rtl_433 -R help 2>&1 | grep -i 'badger' | head -n1 || true)
        if [[ -n $badger ]]; then
            log "Badger decoder in this build: $badger  (config [source] protocol must match)"
        else
            warn "this rtl_433 build lists no Badger ORION decoder; see README"
        fi
    fi
fi

# ---------------------------------------------------------------- hardware watchdog
# The BCM2835 watchdog reboots the Pi when systemd (PID 1) stops petting it for 15 s,
# i.e. on a kernel or PID 1 hang; RebootWatchdogSec bounds a reboot that hangs.
if [[ $SYSTEM_TUNING -eq 0 ]]; then
    log "Hardware watchdog: left to the host (--no-system-tuning)"
elif [[ -e /dev/watchdog ]]; then
    if install_content "$WATCHDOG_DROPIN" 0644 "# installed by water-bacnet: hardware watchdog (docs/HARDWARE.md, recovery step 4)
[Manager]
RuntimeWatchdogSec=15s
RebootWatchdogSec=2min"; then
        log "Enabled the hardware watchdog ($WATCHDOG_DROPIN); re-executing systemd"
        systemctl daemon-reexec || warn "systemctl daemon-reexec failed; the watchdog starts at next boot"
    fi
else
    warn "/dev/watchdog not found; hardware watchdog NOT configured (on a Pi: dtparam=watchdog=on)"
fi

# ---------------------------------------------------------------- journal
# Persistent and size-capped: after a power cut or a watchdog reboot, the previous
# boot's log (journalctl -b -1) is the only record of why. Volatile storage would lose
# exactly those logs. The caps bound SD-card use and wear (README "Logs and SD wear").
if [[ $SYSTEM_TUNING -eq 0 ]]; then
    log "Journal: left to the host (--no-system-tuning)"
elif install_content "$JOURNALD_DROPIN" 0644 "# installed by water-bacnet: keep previous boots' logs, bounded
[Journal]
Storage=persistent
SystemMaxUse=64M
SystemMaxFileSize=8M
MaxRetentionSec=6month"; then
    log "Journal: persistent, capped at 64 MB ($JOURNALD_DROPIN)"
    install -d -m 2755 -o root -g systemd-journal /var/log/journal 2>/dev/null \
        || install -d -m 0755 /var/log/journal
    systemctl restart systemd-journald || warn "could not restart systemd-journald"
    journalctl --flush >/dev/null 2>&1 || true
fi
if systemctl is-active --quiet rsyslog 2>/dev/null; then
    warn "rsyslog is running and copies every message to /var/log too (double SD-card writes); consider: sudo apt purge rsyslog"
fi

# ---------------------------------------------------------------- unattended-upgrades
if [[ $UNATTENDED -eq 1 ]]; then
    apt_install unattended-upgrades
    # Debian-Security only. Raspberry Pi OS has no separate security suite to add:
    # archive.raspberrypi.com has no <codename>-security (checked 2026-10-18), and the
    # armhf base (Raspbian) has none either.
    # shellcheck disable=SC2016  # ${distro_codename} is expanded by unattended-upgrades
    install_content "$UU_DROPIN" 0644 '// installed by water-bacnet (--unattended-upgrades): security updates only
Unattended-Upgrade::Origins-Pattern {
        "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";
};
Unattended-Upgrade::Automatic-Reboot "false";' || true
    install_content "$UU_AUTO" 0644 'APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";' || true
    log "unattended-upgrades: Debian-Security only, automatic reboot off ($UU_DROPIN)"
    if [[ $ARCH == armhf ]]; then
        warn "armhf = Raspbian base, which has no Debian-Security origin: unattended-upgrades will install nothing. Prefer the 64-bit (arm64) OS."
    fi
fi

# ---------------------------------------------------------------- recovery helper
# Root oneshot triggered by $HELPER_DIR/request.json. The request only names an
# action; the helper reads every parameter from $CONFIG and keeps its own rate-limit
# ledger in /var/lib/water-bacnet-helper (root only).
install -d -m 0750 -o "$APP" -g "$APP" "$HELPER_DIR"
HAVE_HELPER=0
if [[ -f $SRC/systemd/$APP-helper.path && -f $SRC/systemd/$APP-helper.service ]]; then
    install -m 0644 "$SRC/systemd/$APP-helper.service" "$HELPER_SERVICE"
    install -m 0644 "$SRC/systemd/$APP-helper.path" "$HELPER_PATH"
    HAVE_HELPER=1
else
    warn "systemd/$APP-helper.{path,service} missing; USB reset / power-cycle recovery disabled"
fi

# ---------------------------------------------------------------- service
log "Installing and (re)starting $APP.service and $APP-portal.service"
install -m 0644 "$SRC/systemd/$APP.service" "$UNIT"
install -m 0644 "$SRC/systemd/$APP-portal.service" "$PORTAL_UNIT"
# From here on nothing aborts the script: the self-check below is the useful output.
set +e
systemctl daemon-reload || warn "systemctl daemon-reload failed"
systemctl enable "$APP.service" "$APP-portal.service" >/dev/null || warn "systemctl enable failed"
if [[ $HAVE_HELPER -eq 1 ]]; then
    systemctl enable --now "$APP-helper.path" >/dev/null || warn "could not enable $APP-helper.path"
fi
# --no-block: with Type=notify a restart waits for READY=1 (up to 60 s for the bind
# address); the status below shows where it got to.
systemctl restart --no-block "$APP.service" || warn "could not restart $APP.service"
systemctl restart "$APP-portal.service" || warn "could not restart $APP-portal.service"

echo
log "Self-check: python -m water_bacnet --check"
runuser -u "$APP" -- env PYTHONPATH="$PREFIX" "$VENV/bin/python" -m water_bacnet \
    --config "$CONFIG" --check || warn "--check reported a problem (see above)"

echo
for _ in $(seq 1 30); do
    systemctl is-active --quiet "$APP.service" && break
    sleep 1
done
systemctl --no-pager --lines=10 status "$APP.service" || true

echo
if [[ $REPLUG -eq 1 ]]; then
    warn "UNPLUG AND REPLUG the RTL-SDR dongle (or reboot) so the DVB driver releases it."
fi
METER_ID=$(read_key "$CONFIG" meter id 0)
if [[ ! $METER_ID =~ ^[0-9]+$ || $METER_ID -eq 0 ]]; then
    warn "[meter] id is not set, so the BACnet service will not start yet. Find the on-air id with: rtl_433 -f 916.45M -s 1600k -R 223 -F json  (the Badger-ORION packet whose \"id\" = the endpoint's stamped serial & 0xFFFFFF), then set it on the web portal (Settings) or in $CONFIG."
fi
if [[ ${BIND,,} == auto ]]; then
    warn "[bacnet] bind = \"auto\": the BACnet address follows the default route. With a DHCP reservation set it explicitly, e.g. bind = \"$(hostname -I 2>/dev/null | awk '{print $1}')/24\" (check the prefix)."
fi
log "Done. Logs: journalctl -u $APP -f    Config: $CONFIG"
PORTAL_PORT=$(read_key "$CONFIG" portal port 8080)
[[ $PORTAL_PORT =~ ^[0-9]+$ ]] || PORTAL_PORT=8080
HOST_IP=$(read_key "$CONFIG" portal listen 0.0.0.0)
[[ $HOST_IP == 0.0.0.0 || -z $HOST_IP ]] && HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
log "Web portal: http://${HOST_IP:-<this-pi>}:$PORTAL_PORT/  (plain HTTP, LAN only)"
if [[ -n $NEW_PASSWORD ]]; then
    echo
    printf '\033[1m    Portal admin password: %s\033[0m\n' "$NEW_PASSWORD"
    echo "    (saved in $INITIAL_PW; change it on the portal's Password page)"
fi
exit 0
