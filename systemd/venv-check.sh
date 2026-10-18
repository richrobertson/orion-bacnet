#!/bin/sh
# ExecStartPre=+ helper for water-bacnet.service (runs as root), also used by install.sh.
#
# Makes sure /opt/water-bacnet/venv works with the CURRENT system python3 and has the
# pinned bacpypes3. If not -- typically after a Debian release upgrade replaced
# python3.11 with python3.13, which leaves the venv importing nothing -- it rebuilds
# the venv OFFLINE from the vendored wheel in /opt/water-bacnet/vendor, after checking
# the wheel against vendor/SHA256SUMS. No internet is needed or used.
#
# Exit 0 = venv OK (or repaired); 1 = broken and could not be repaired (the start
# fails, it is logged, and Restart=always tries again in 10 s).
# Usage: venv-check.sh [PREFIX]
PREFIX=${1:-/opt/water-bacnet}
VENV=$PREFIX/venv
VENDOR=$PREFIX/vendor
PIN_NAME=bacpypes3
PIN_VERSION=0.0.110
SYS_PY=${WATER_BACNET_SYSTEM_PYTHON:-python3}

say() { echo "water-bacnet venv-check: $*" >&2; }

want=$("$SYS_PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || {
    say "cannot run $SYS_PY; leaving the venv alone"
    exit 0
}

reason=""
if [ ! -x "$VENV/bin/python" ]; then
    reason="no $VENV/bin/python"
else
    # pyvenv.cfg: "version = 3.11.2" (or version_info = ...). Compare major.minor only:
    # a patch update keeps the venv working, a minor update breaks site-packages.
    have=$(sed -n 's/^version\(_info\)\{0,1\} *= *\([0-9][0-9]*\.[0-9][0-9]*\).*/\2/p' \
        "$VENV/pyvenv.cfg" 2>/dev/null | head -n 1)
    if [ "$have" != "$want" ]; then
        reason="venv was built for Python ${have:-unknown}, system python3 is $want"
    elif ! "$VENV/bin/python" -c "import importlib.metadata as m, sys, bacpypes3
sys.exit(0 if m.version('$PIN_NAME') == '$PIN_VERSION' else 1)" 2>/dev/null; then
        reason="import $PIN_NAME failed or it is not version $PIN_VERSION"
    fi
fi
[ -z "$reason" ] && exit 0

say "$reason -- rebuilding $VENV offline from $VENDOR"
if [ ! -f "$VENDOR/SHA256SUMS" ]; then
    say "missing $VENDOR/SHA256SUMS; cannot rebuild"
    exit 1
fi
WHEEL=$(awk 'NR == 1 { print $2 }' "$VENDOR/SHA256SUMS")
SHA=$(awk 'NR == 1 { print $1 }' "$VENDOR/SHA256SUMS")
case "$WHEEL" in
    "$PIN_NAME-$PIN_VERSION-"*.whl) ;;
    *) say "unexpected wheel in SHA256SUMS: $WHEEL"; exit 1 ;;
esac
if ! (cd "$VENDOR" && sha256sum -c --status SHA256SUMS); then
    say "$VENDOR/$WHEEL does not match SHA256SUMS; refusing to install it"
    exit 1
fi

# Rebuild in place: the old venv is already unusable (wrong Python, or no / the wrong
# bacpypes3), so there is nothing to preserve, and venvs are not relocatable.
if "$SYS_PY" -m venv --clear "$VENV" 2>/dev/null; then
    REQ=$(mktemp) || exit 1
    echo "$PIN_NAME==$PIN_VERSION --hash=sha256:$SHA" >"$REQ"
    "$VENV/bin/python" -m pip install --disable-pip-version-check --no-input -q \
        --no-index --find-links "$VENDOR" --require-hashes -r "$REQ" >&2
    rc=$?
    rm -f "$REQ"
else
    # No ensurepip for this Python (python3-venv not installed for it). bacpypes3 is a
    # pure-Python wheel, so unpacking it into site-packages is a complete install.
    say "python3 -m venv failed (python3-venv missing for $want?); building without pip"
    "$SYS_PY" -m venv --clear --without-pip "$VENV" || { say "venv --without-pip failed too"; exit 1; }
    SITE=$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])') || exit 1
    "$VENV/bin/python" -m zipfile -e "$VENDOR/$WHEEL" "$SITE"
    rc=$?
fi
if [ "$rc" -ne 0 ] || ! "$VENV/bin/python" -c 'import bacpypes3' 2>/dev/null; then
    say "rebuild FAILED (rc=$rc); the service cannot start until this is fixed"
    exit 1
fi
say "venv rebuilt for Python $want with $PIN_NAME $PIN_VERSION"
exit 0
