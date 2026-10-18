"""Root helper (water_bacnet.helper) and usbctl primitives, with faked hardware."""

import json
import os
import subprocess

import pytest

from water_bacnet import helper as helper_mod
from water_bacnet import usbctl
from water_bacnet.helper import Helper, load_settings

T0 = 1_790_000_000.0

CONFIG = """
[source]
device = ":00000001"
[recovery]
usb_reset = true
power_cycle = true
reboot = {reboot}
uhubctl_path = "/usr/sbin/uhubctl"
hub_location = "1-1"
hub_port = "2"
cycle_delay_s = 7
usb_vid_pid = "0bda:2838"
max_power_cycles_per_day = 2
max_reboots_per_week = 1
"""


class Ops:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def _maybe_fail(self, name):
        if self.fail == name:
            raise RuntimeError(f"{name} exploded")

    def find_dongle(self, vid_pid, serial=None):
        self.calls.append(("find_dongle", vid_pid, serial))
        self._maybe_fail("find_dongle")
        return (1, 3)

    def usb_reset(self, bus, dev):
        self.calls.append(("usb_reset", bus, dev))
        self._maybe_fail("usb_reset")
        return f"reset {bus}/{dev}"

    def power_cycle(self, path, loc, port, delay):
        self.calls.append(("power_cycle", path, loc, port, delay))
        self._maybe_fail("power_cycle")
        return "cycled"

    def request_reboot(self):
        self.calls.append(("reboot",))
        self._maybe_fail("reboot")
        return "ok"


class Rig:
    def __init__(self, tmp_path, reboot=False, config=None, fail=None):
        self.conf = tmp_path / "config.toml"
        self.conf.write_text(config if config is not None else CONFIG.format(reboot=str(reboot).lower()))
        self.dir = tmp_path / "helper"
        self.dir.mkdir()
        self.ledger = tmp_path / "root" / "ledger.json"
        self.now = T0
        self.ops = Ops(fail)
        self.h = Helper(str(self.conf), str(self.dir), str(self.ledger), ops=self.ops,
                        clock=lambda: self.now)

    def request(self, action="usb_reset", rid="abc123", **extra):
        d = {"id": rid, "action": action, "ts": self.now, **extra}
        (self.dir / "request.json").write_text(json.dumps(d))

    def run(self):
        rc = self.h.run()
        assert not (self.dir / "request.json").exists(), "request left behind (re-trigger loop)"
        return rc

    def response(self):
        return json.loads((self.dir / "response.json").read_text())

    def bad_files(self):
        return sorted(p.name for p in self.dir.glob("request.json.bad-*"))


def test_usb_reset_success(tmp_path):
    rig = Rig(tmp_path)
    rig.request("usb_reset", rid="id-1")
    assert rig.run() == 0
    assert rig.ops.calls == [("find_dongle", "0bda:2838", "00000001"), ("usb_reset", 1, 3)]
    r = rig.response()
    assert r == {"id": "id-1", "ok": True, "detail": "reset 1/3", "ts": T0}
    assert rig.bad_files() == []
    assert json.loads(rig.ledger.read_text())["usb_reset"] == [T0]
    assert oct(rig.ledger.stat().st_mode & 0o777) == "0o600"
    leftovers = [p.name for p in rig.dir.iterdir() if p.name != "response.json"]
    assert leftovers == []


def test_no_request_is_a_noop(tmp_path):
    rig = Rig(tmp_path)
    assert rig.run() == 0
    assert rig.ops.calls == []
    assert not (rig.dir / "response.json").exists()


def test_action_not_whitelisted_is_rejected_and_renamed(tmp_path):
    rig = Rig(tmp_path)
    rig.request("rm -rf /")
    assert rig.run() == 0
    assert rig.ops.calls == []
    r = rig.response()
    assert r["ok"] is False and "not allowed" in r["detail"] and r["id"] == "abc123"
    assert rig.bad_files() == [f"request.json.bad-{int(T0)}"]


@pytest.mark.parametrize("body", ["not json {", "null", "[]", '"usb_reset"', "1e400",
                                  '{"action": "usb_reset"}',
                                  '{"id": "../../etc/x", "action": "usb_reset"}',
                                  '{"id": 5, "action": "usb_reset"}'])
def test_bad_request_renamed_never_acted_on(tmp_path, body):
    rig = Rig(tmp_path)
    (rig.dir / "request.json").write_text(body)
    assert rig.run() == 0
    assert rig.ops.calls == []
    assert rig.response()["ok"] is False
    assert len(rig.bad_files()) == 1


def test_symlinked_request_is_not_followed(tmp_path):
    rig = Rig(tmp_path)
    target = tmp_path / "secret.json"
    target.write_text(json.dumps({"id": "x1", "action": "usb_reset"}))
    os.symlink(target, rig.dir / "request.json")
    assert rig.run() == 0
    assert rig.ops.calls == []
    assert target.exists()  # the link was moved aside, not its target
    assert len(rig.bad_files()) == 1


def test_oversized_request_rejected(tmp_path):
    rig = Rig(tmp_path)
    rig.request("usb_reset", pad="x" * 10000)
    assert rig.run() == 0
    assert rig.ops.calls == []


def test_parameters_in_request_are_ignored(tmp_path):
    rig = Rig(tmp_path)
    rig.request("power_cycle", uhubctl_path="/tmp/evil", hub_location="9-9", hub_port="1,2,3",
                delay=0, cycle_delay_s=1, usb_vid_pid="dead:beef", argv=["sh", "-c", "id"])
    assert rig.run() == 0
    assert rig.ops.calls == [("power_cycle", "/usr/sbin/uhubctl", "1-1", "2", 7)]
    assert rig.response()["ok"] is True


def test_usb_reset_rate_limit_ten_minutes(tmp_path):
    rig = Rig(tmp_path)
    rig.request("usb_reset", rid="a")
    rig.run()
    rig.now += 300
    rig.request("usb_reset", rid="b")
    rig.run()
    r = rig.response()
    assert r["id"] == "b" and r["ok"] is False and "rate limited" in r["detail"]
    rig.now += 301
    rig.request("usb_reset", rid="c")
    rig.run()
    assert rig.response()["ok"] is True
    assert [c[0] for c in rig.ops.calls].count("usb_reset") == 2


def test_power_cycle_daily_limit_from_config(tmp_path):
    rig = Rig(tmp_path)
    oks = []
    for i in range(3):
        rig.request("power_cycle", rid=f"p{i}")
        rig.run()
        oks.append(rig.response()["ok"])
        rig.now += 3600
    assert oks == [True, True, False]
    rig.now = T0 + 86400 + 1  # first one has aged out
    rig.request("power_cycle", rid="p9")
    rig.run()
    assert rig.response()["ok"] is True


def test_ledger_survives_clock_step_back(tmp_path):
    """Entries "in the future" after a backwards clock step still count (within a window)."""
    rig = Rig(tmp_path)
    rig.request("usb_reset", rid="a")
    rig.run()
    rig.now -= 300  # clock stepped back 5 min
    rig.request("usb_reset", rid="b")
    rig.run()
    assert rig.response()["ok"] is False
    for i in range(2):
        rig.request("power_cycle", rid=f"p{i}")
        rig.run()
    rig.now -= 20 * 3600  # back most of a day: both power cycles still count
    rig.request("power_cycle", rid="p3")
    rig.run()
    assert rig.response()["ok"] is False


def test_corrupt_ledger_starts_fresh(tmp_path):
    rig = Rig(tmp_path)
    rig.ledger.parent.mkdir(parents=True)
    rig.ledger.write_text('{"usb_reset": [1e400, "x", null, true]')
    rig.request("usb_reset")
    rig.run()
    assert rig.response()["ok"] is True


def test_ledger_unwritable_fails_closed(tmp_path):
    rig = Rig(tmp_path)
    rig.ledger.parent.mkdir(parents=True)
    rig.ledger.parent.chmod(0o500)
    try:
        rig.request("usb_reset")
        rig.run()
    finally:
        rig.ledger.parent.chmod(0o700)
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    assert rig.ops.calls == []
    assert rig.response()["ok"] is False


def test_reboot_disabled_by_default(tmp_path):
    rig = Rig(tmp_path, config="")
    rig.request("reboot")
    rig.run()
    r = rig.response()
    assert r["ok"] is False and "disabled" in r["detail"]
    assert rig.ops.calls == []


def test_reboot_weekly_limit(tmp_path):
    rig = Rig(tmp_path, reboot=True)
    rig.request("reboot", rid="r1")
    rig.run()
    assert rig.response() == {"id": "r1", "ok": True, "detail": "reboot requested", "ts": T0}
    rig.now += 3 * 86400
    rig.request("reboot", rid="r2")
    rig.run()
    assert rig.response()["ok"] is False
    assert rig.ops.calls == [("reboot",)]


def test_action_failure_reported(tmp_path):
    rig = Rig(tmp_path, fail="usb_reset")
    rig.request("usb_reset")
    assert rig.run() == 0
    r = rig.response()
    assert r["ok"] is False and "exploded" in r["detail"]


def test_invalid_config_values_fall_back_to_defaults(tmp_path):
    conf = tmp_path / "c.toml"
    conf.write_text('[recovery]\nhub_location = "1-1; reboot"\ncycle_delay_s = 600\n'
                    'usb_vid_pid = 7\nreboot = "yes"\nuhubctl_path = "-x"\n')
    s = load_settings(str(conf))
    assert s.hub_location == "1-1" and s.cycle_delay_s == 5 and s.usb_vid_pid == "0bda:2838"
    assert s.reboot is False and s.uhubctl_path == "uhubctl"
    (tmp_path / "broken.toml").write_text("[recovery\n")
    s = load_settings(str(tmp_path / "broken.toml"))
    assert s.reboot is False and s.usb_reset is True
    # default device "" = first dongle: no serial to match on
    assert load_settings(str(tmp_path / "missing.toml")).serial is None


def test_bad_files_are_capped(tmp_path):
    rig = Rig(tmp_path)
    for i in range(helper_mod.KEEP_BAD + 5):
        rig.now = T0 + i
        (rig.dir / "request.json").write_text("junk")
        os.utime(rig.dir / "request.json", (T0 + i, T0 + i))
        rig.run()
    assert len(rig.bad_files()) == helper_mod.KEEP_BAD


def test_main_uses_args(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    rig.request("usb_reset")
    # main() builds Helper with its default ops (usbctl): swap the defaults for fakes
    monkeypatch.setattr(helper_mod.Helper.__init__, "__defaults__",
                        (helper_mod.DEFAULT_CONFIG, helper_mod.DEFAULT_REQUEST_DIR,
                         helper_mod.DEFAULT_LEDGER, rig.ops, lambda: T0))
    rc = helper_mod.main(["--config", str(rig.conf), "--request-dir", str(rig.dir),
                          "--ledger", str(rig.ledger)])
    assert rc == 0
    assert rig.response()["ok"] is True
    assert ("usb_reset", 1, 3) in rig.ops.calls


# ---- usbctl ------------------------------------------------------------------------

def _sysfs(root, name, vid, pid, bus, dev, serial=None):
    d = root / name
    d.mkdir(parents=True)
    (d / "idVendor").write_text(vid + "\n")
    (d / "idProduct").write_text(pid + "\n")
    (d / "busnum").write_text(f"{bus}\n")
    (d / "devnum").write_text(f"{dev}\n")
    if serial is not None:
        (d / "serial").write_text(serial + "\n")


def test_find_dongle(tmp_path):
    root = tmp_path / "sys"
    _sysfs(root, "1-1", "2109", "3431", 1, 2)  # the Pi 4 VL805 hub
    _sysfs(root, "1-1.3", "0bda", "2838", 1, 5, serial="00000002")
    _sysfs(root, "1-1.4", "0bda", "2838", 1, 7, serial="00000001")
    (root / "1-1.4:1.0").mkdir()  # an interface directory, ignored
    assert usbctl.find_dongle("0bda:2838", "00000001", str(root)) == (1, 7)
    assert usbctl.find_dongle("0BDA:2838", "00000002", str(root)) == (1, 5)
    with pytest.raises(LookupError):
        usbctl.find_dongle("0bda:2838", None, str(root))  # ambiguous
    with pytest.raises(LookupError):
        usbctl.find_dongle("0bda:2838", "nope", str(root))
    with pytest.raises(LookupError):
        usbctl.find_dongle("1d50:604b", None, str(root))


def test_find_dongle_single_match_despite_serial(tmp_path):
    root = tmp_path / "sys"
    _sysfs(root, "1-1.2", "0bda", "2838", 1, 9, serial="00000002")
    assert usbctl.find_dongle("0bda:2838", "00000001", str(root)) == (1, 9)


def test_usb_reset_ioctl(tmp_path):
    devroot = tmp_path / "dev"
    (devroot / "001").mkdir(parents=True)
    (devroot / "001" / "007").write_text("")
    seen = []
    detail = usbctl.usb_reset(1, 7, str(devroot), ioctl=lambda fd, req, arg: seen.append((req, arg)))
    assert seen == [(0x5514, 0)]
    assert "001/007" in detail
    with pytest.raises(OSError):
        usbctl.usb_reset(1, 8, str(devroot), ioctl=lambda *a: None)


def test_power_cycle_argv_and_errors():
    seen = []

    def run_ok(argv, **kw):
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, stdout=b"Sent power cycle request\n")
    usbctl.power_cycle("uhubctl", "1-1", "", 5, run=run_ok)
    usbctl.power_cycle("/usr/sbin/uhubctl", "2", "1,3", 9, run=run_ok)
    assert seen[0][0] == ["uhubctl", "-l", "1-1", "-a", "cycle", "-d", "5"]
    assert seen[1][0] == ["/usr/sbin/uhubctl", "-l", "2", "-p", "1,3", "-a", "cycle", "-d", "9"]
    assert seen[0][1]["timeout"] == 35 and "shell" not in seen[0][1]

    def run_fail(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout=b"No compatible devices detected!\n")
    with pytest.raises(RuntimeError, match="No compatible"):
        usbctl.power_cycle("uhubctl", "1-1", "", 5, run=run_fail)

    def run_timeout(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])
    with pytest.raises(RuntimeError, match="timed out"):
        usbctl.power_cycle("uhubctl", "1-1", "", 5, run=run_timeout)

    def run_missing(argv, **kw):
        raise FileNotFoundError(argv[0])
    with pytest.raises(RuntimeError, match="cannot run"):
        usbctl.power_cycle("uhubctl", "1-1", "", 5, run=run_missing)


def test_request_reboot_argv():
    seen = []
    usbctl.request_reboot(run=lambda argv, **kw: seen.append(argv) or
                          subprocess.CompletedProcess(argv, 0, stdout=b""))
    assert seen == [["systemctl", "reboot"]]
