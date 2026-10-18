"""Web configuration portal: auth, CSRF, validation, safe apply + rollback, API."""

import asyncio
import dataclasses
import hashlib
import http.client
import io
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import pytest

from conftest import METER_ID, ROOT
from water_bacnet import __main__ as wmain
from water_bacnet import portal as wp
from water_bacnet.config import dump_toml, load_config, loads_config, problems, validate

PASSWORD = "correct horse battery"


# ---------------------------------------------------------------------------------
# fixtures / helpers

class Client:
    """Tiny HTTP client with a cookie jar and no redirect following."""

    def __init__(self, port):
        self.port = port
        self.cookies = {}

    def request(self, method, path, form=None, headers=None, raw_body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        body = None
        if form is not None:
            body = urlencode(form).encode()
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        if raw_body is not None:
            body = raw_body
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        for v in resp.headers.get_all("Set-Cookie") or []:
            name, _, rest = v.partition("=")
            value = rest.split(";")[0]
            if "Max-Age=0" in v:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value
        conn.close()
        return resp.status, resp.headers, data.decode("utf-8", "replace")

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, form, **kw):
        return self.request("POST", path, form=form, **kw)

    def csrf_from(self, html):
        m = re.search(r'name="csrf" value="([^"]+)"', html)
        assert m, "no csrf field in page"
        return m.group(1)

    def login(self, password=PASSWORD):
        status, _, html = self.get("/login")
        assert status == 200
        return self.post("/login", {"csrf": self.csrf_from(html), "password": password})


@pytest.fixture
def env(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    cfg_text = (ROOT / "config.example.toml").read_text().replace(
        'state_dir = "/var/lib/water-bacnet"', f'state_dir = "{state_dir}"').replace(
        "\nid = 0\n", f"\nid = {METER_ID}\n")
    config = etc / "config.toml"
    config.write_text(cfg_text)
    (etc / "config.toml.last-good").write_text(cfg_text)
    portal = wp.Portal(config, runtime_dir=str(run), use_systemd=False,
                       apply_timeout_s=3.0, healthy_after_s=0.3,
                       on_portal_change=lambda: None)
    wp.write_password_file(portal.passwd_path, PASSWORD)
    server = wp.PortalServer(("127.0.0.1", 0), portal)
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True)
    t.start()
    e = dataclasses.make_dataclass("Env", ["portal", "port", "etc", "config", "state_dir",
                                           "run", "client"])(
        portal, server.server_address[1], etc, config, state_dir, run,
        Client(server.server_address[1]))
    try:
        yield e
    finally:
        server.shutdown()
        server.server_close()


class FakeMain:
    """Stands in for the BACnet service: heartbeats for whatever config.toml holds."""

    def __init__(self, config: Path, run: Path, healthy=True):
        self.config, self.run, self.healthy = config, run, healthy
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join(2)

    def _loop(self):
        sha, started = None, None
        while not self._stop.is_set():
            data = self.config.read_bytes()
            h = hashlib.sha256(data).hexdigest()
            if h != sha:
                sha, started = h, time.time()
            d = {"pid": os.getpid(), "state": "running" if self.healthy else "failed",
                 "loaded_from": "config", "config_sha256": sha, "started": started,
                 "ready_at": started, "heartbeat": time.time(),
                 "error": None if self.healthy else "OSError: [Errno 49] cannot bind"}
            tmp = self.run / "main-status.json.tmp"
            tmp.write_text(json.dumps(d))
            os.replace(tmp, self.run / "main-status.json")
            self._stop.wait(0.05)


def settings_form(client, **changes):
    status, _, html = client.get("/settings")
    assert status == 200, html
    form = {"csrf": client.csrf_from(html)}
    for m in re.finditer(r'<input type="text" id="[^"]+" name="([^"]+)" value="([^"]*)"', html):
        form[m.group(1)] = _unescape(m.group(2))
    for m in re.finditer(r'<input type="checkbox" id="[^"]+" name="([^"]+)"( checked)?', html):
        if m.group(2):
            form[m.group(1)] = "on"
    for m in re.finditer(r'<select id="[^"]+" name="([^"]+)"[^>]*>(.*?)</select>', html):
        sel = re.search(r"<option selected>([^<]*)</option>", m.group(2))
        form[m.group(1)] = sel.group(1)
    for m in re.finditer(r'<textarea id="[^"]+" name="([^"]+)"[^>]*>(.*?)</textarea>', html, re.S):
        form[m.group(1)] = _unescape(m.group(2))
    for k, v in changes.items():
        if v is None:
            form.pop(k, None)
        else:
            form[k] = v
    return form


def _unescape(s):
    import html
    return html.unescape(s)


# ---------------------------------------------------------------------------------
# config helpers

def test_dump_toml_round_trips_example_and_odd_strings():
    cfg = load_config(ROOT / "config.example.toml")
    assert loads_config(dump_toml(cfg)) == dataclasses.replace(cfg, path=None)
    odd = dataclasses.replace(
        cfg, bacnet=dataclasses.replace(cfg.bacnet, description='q"uote \\ back\nnl\t\x01 é',
                                        object_names={"analogInput:1": 'Reg "A"'}),
        source=dataclasses.replace(cfg.source, extra_args=("-g", "40", "a b")),
        path=None)
    assert loads_config(dump_toml(odd)) == odd


def test_validate_rejects_values_load_config_accepts():
    base = dump_toml(load_config(None))
    bad = base.replace('frequency = "916.45M"', 'frequency = "lots"').replace(
        'hub_location = "1-1"', 'hub_location = "x;y"')
    loads_config(bad)  # lenient loader: fine
    with pytest.raises(ValueError) as e:
        validate(bad)
    assert "frequency" in str(e.value) and "hub_location" in str(e.value)
    assert problems(loads_config(f"[meter]\nid = {METER_ID}\n")) == []
    # A bind that does not parse is refused already at load time (the service would
    # otherwise only find out when it tries to bind).
    with pytest.raises(ValueError, match="bind"):
        loads_config(base.replace('bind = "auto"', 'bind = "192.0.2.300/24"'))


def test_bad_portal_value_does_not_break_main_config():
    cfg = loads_config('[portal]\nport = "eighty"\n[bacnet]\nport = 47809\n')
    assert cfg.portal.port == 8080 and cfg.bacnet.port == 47809


# ---------------------------------------------------------------------------------
# auth

def test_password_hash_format_and_verify():
    h = wp.hash_password("pw12345678")
    algo, iters, _, _ = h.split("$")
    assert algo == "pbkdf2_sha256" and int(iters) >= 200_000
    assert wp.verify_password("pw12345678", h)
    assert not wp.verify_password("pw12345679", h)
    assert not wp.verify_password("x", "garbage")


def test_password_file_is_private(env):
    assert (env.portal.passwd_path.stat().st_mode & 0o777) == 0o600


def test_login_and_settings_access(env):
    c = env.client
    status, headers, _ = c.get("/settings")
    assert status == 303 and headers["Location"] == "/login"
    status, _, html = c.login("wrong password")
    assert status == 401 and "Wrong password" in html
    assert "wb_session" not in c.cookies
    status, headers, _ = c.login()
    assert status == 303 and headers["Location"] == "/settings"
    cookie = [v for v in headers.get_all("Set-Cookie") if v.startswith("wb_session=")][0]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    status, _, html = c.get("/settings")
    assert status == 200 and 'name="meter.id" value="7654321"' in html


def test_csrf_rejected(env):
    c = env.client
    c.login()
    form = settings_form(c)
    form["csrf"] = "forged"
    status, _, html = c.post("/settings", form)
    assert status == 403 and "CSRF" in html
    form.pop("csrf")
    assert c.post("/settings", form)[0] == 403
    # Login form needs its pre-session token too.
    c2 = wp_client(env)
    assert c2.post("/login", {"password": PASSWORD})[0] == 403
    # Cross-site Origin refused even with a valid token.
    form = settings_form(c)
    status, _, _ = c.post("/settings", form, headers={"Origin": "http://evil.example"})
    assert status == 403


def wp_client(env):
    return Client(env.port)


def test_login_rate_limited(env):
    c = env.client
    for _ in range(wp.LOGIN_MAX_PER_IP):
        assert c.login("nope")[0] == 401
    status, _, html = c.login()  # even the right password is refused for now
    assert status == 429 and "Too many" in html


def test_change_password(env):
    c = env.client
    c.login()
    _, _, html = c.get("/password")
    tok = c.csrf_from(html)
    assert c.post("/password", {"csrf": tok, "current": "bad", "new": "newpass123",
                                "again": "newpass123"})[0] == 400
    assert c.post("/password", {"csrf": tok, "current": PASSWORD, "new": "short",
                                "again": "short"})[0] == 400
    status, headers, _ = c.post("/password", {"csrf": tok, "current": PASSWORD,
                                              "new": "newpass123", "again": "newpass123"})
    assert status == 303
    assert c.get("/settings")[0] == 200  # current session re-issued
    c2 = wp_client(env)
    assert c2.login(PASSWORD)[0] == 401
    assert c2.login("newpass123")[0] == 303


def test_set_password_cli(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    monkeypatch.setattr("sys.stdin", io.StringIO("cli-password-1\n"))
    assert wp.main(["--config", str(cfg), "--set-password", "--password-stdin"]) == 0
    assert wp.verify_password("cli-password-1", (tmp_path / "portal.passwd").read_text())
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert wp.main(["--config", str(cfg), "--set-password", "--password-stdin"]) == 1


# ---------------------------------------------------------------------------------
# settings: validation, save, rollback

def test_validation_rejects_bad_values(env):
    c = env.client
    c.login()
    before = env.config.read_bytes()
    for field, value, needle in [("bacnet.port", "70000", "port"),
                                 ("meter.id", "abc", "whole number"),
                                 ("source.frequency", "916 MHz", "frequency"),
                                 ("bacnet.bind", "999.1.1.1/24", "bind")]:
        status, _, html = c.post("/settings", settings_form(c, **{field: value}))
        assert status == 400, field
        assert "Not saved" in html and needle in html, field
        assert 'value="' + value + '"' in html  # the form keeps what was typed
    assert env.config.read_bytes() == before
    assert env.portal.apply_state["state"] == "idle"


def test_successful_save_writes_valid_toml(env):
    c = env.client
    c.login()
    with FakeMain(env.config, env.run):
        form = settings_form(c, **{"meter.stale_after_s": "1200", "bacnet.device_instance": "2802",
                                   "source.mode": "state_file", "storage.raw_log": None,
                                   "source.extra_args": "-g 40 -M 'level x'",
                                   "bacnet.object_names": "analogInput:1 = Register A"})
        status, headers, _ = c.post("/settings", form)
        assert status == 303
        state = env.portal.wait_apply(10)
    assert state["state"] == "applied", state
    cfg = load_config(env.config)
    assert cfg.meter.stale_after_s == 1200.0
    assert cfg.bacnet.device_instance == 2802
    assert cfg.source.mode == "state_file"
    assert cfg.storage.raw_log is False
    assert cfg.source.extra_args == ("-g", "40", "-M", "level x")
    assert cfg.bacnet.object_names == {"analogInput:1": "Register A"}
    # Reads back identically: every other key unchanged from the example.
    ex = load_config(ROOT / "config.example.toml")
    R = dataclasses.replace
    want = R(ex, path=str(env.config),
             meter=R(ex.meter, id=METER_ID, stale_after_s=1200.0),
             source=R(ex.source, mode="state_file", extra_args=("-g", "40", "-M", "level x")),
             storage=R(ex.storage, state_dir=str(env.state_dir), raw_log=False),
             bacnet=R(ex.bacnet, device_instance=2802,
                      object_names={"analogInput:1": "Register A"}))
    assert cfg == want
    assert loads_config(dump_toml(cfg), str(env.config)) == cfg
    # last-good follows a confirmed-good config; the previous file was backed up.
    assert (env.etc / "config.toml.last-good").read_bytes() == env.config.read_bytes()
    backups = list(env.etc.glob("config.toml.bak-*"))
    assert len(backups) == 1 and "[meter]" in backups[0].read_text()
    assert not env.portal.pending_path.exists()
    _, _, html = c.get("/settings")
    assert "Applied" in html


def test_rollback_when_main_never_reports_healthy(env):
    c = env.client
    c.login()
    good = (env.etc / "config.toml.last-good").read_bytes()
    status, _, _ = c.post("/settings", settings_form(c, **{"bacnet.port": "47900"}))
    assert status == 303
    assert load_config(env.config).bacnet.port == 47900  # written immediately...
    state = env.portal.wait_apply(15)
    assert state["state"] == "rolled_back", state
    assert "did not report healthy" in state["error"]
    assert env.config.read_bytes() == good  # ...and rolled back
    assert load_config(env.etc / "config.toml.rejected").bacnet.port == 47900
    _, _, html = c.get("/settings")
    assert "did not work" in html


def test_rollback_when_main_fails_on_new_config(env):
    c = env.client
    c.login()
    good = (env.etc / "config.toml.last-good").read_bytes()
    with FakeMain(env.config, env.run, healthy=False):
        c.post("/settings", settings_form(c, **{"bacnet.bind": "198.51.100.9/24"}))
        state = env.portal.wait_apply(15)
    assert state["state"] == "rolled_back"
    assert "cannot bind" in state["error"]
    assert env.config.read_bytes() == good


def test_backups_are_capped(env):
    for i in range(wp.BACKUPS_KEEP + 3):
        env.portal._backup(f"# {i}\n".encode())
    assert len(list(env.etc.glob("config.toml.bak-*"))) == wp.BACKUPS_KEEP


def test_unchanged_save_is_a_noop(env):
    c = env.client
    c.login()
    before = env.config.read_bytes()
    assert c.post("/settings", settings_form(c))[0] == 303
    assert env.portal.apply_state["state"] == "unchanged"
    assert env.config.read_bytes() == before


def test_resume_pending_after_portal_restart(env):
    data = dump_toml(dataclasses.replace(load_config(env.config), path=None)).encode()
    wp.atomic_write(env.config, data)
    marker = {"sha256": hashlib.sha256(data).hexdigest(), "started": time.time(),
              "verify": True}
    env.portal.pending_path.write_text(json.dumps(marker))
    with FakeMain(env.config, env.run):
        env.portal.resume_pending()
        state = env.portal.wait_apply(10)
    assert state["state"] == "applied"
    assert not env.portal.pending_path.exists()


# ---------------------------------------------------------------------------------
# status

def _write_state(env, age_s=60.0, file_age_s=5.0):
    updated = datetime.fromtimestamp(time.time() - file_age_s, timezone.utc).isoformat()
    (env.state_dir / "state.json").write_text(json.dumps({
        "updated": updated, "meter_id": METER_ID, "volume": 41774.0,
        "volume_units": "cubic_feet", "flow_per_hour": 0.5, "flow_recent_per_hour": 0.0,
        "seconds_since_packet": age_s, "snr": 16.7, "rssi": -8.6, "freq": 915.993,
        "packets_total": 12114, "packets_per_hour": 21.65, "rtl433_restarts": 2,
        "history": []}))


def test_api_status_json(env):
    _write_state(env)
    status, headers, body = env.client.get("/api/status")
    assert status == 200 and headers["Content-Type"] == "application/json"
    d = json.loads(body)
    assert d["feed"] == "fresh"
    assert d["reading"]["volume_cuft"] == 41774.0
    assert d["reading"]["volume_gal"] == pytest.approx(41774.0 * 7.480519, abs=0.01)
    assert 64 <= d["reading"]["seconds_since_packet"] < 80  # live-adjusted
    assert d["reading"]["rtl433_restarts"] == 2
    assert d["config"]["loaded_from"] == "config"
    assert d["main_service"]["active"] == "unknown"
    assert d["version"]


def test_status_page_handles_missing_and_stale_state(env):
    status, _, html = env.client.get("/")
    assert status == 200 and "STALE" in html and "missing" in html
    assert 'http-equiv="refresh"' in html
    _write_state(env, age_s=2000)
    d = json.loads(env.client.get("/api/status")[2])
    assert d["feed"] == "stale" and "no packet" in d["reading"]["error"]
    (env.state_dir / "state.json").write_text("{not json")
    d = json.loads(env.client.get("/api/status")[2])
    assert d["feed"] == "stale" and "invalid" in d["reading"]["error"]


def test_status_page_with_broken_config_uses_last_good(env):
    env.config.write_text("[meter\nid = ")
    status, _, html = env.client.get("/")
    assert status == 200 and "cannot be used" in html
    d = json.loads(env.client.get("/api/status")[2])
    assert d["config"]["loaded_from"] == "last-good"


def test_private_status_requires_login(env):
    text = env.config.read_text().replace("public_status = true", "public_status = false")
    env.config.write_text(text)
    c = env.client
    assert c.get("/api/status")[0] == 401
    assert c.get("/")[0] == 303
    c.login()
    assert c.get("/api/status")[0] == 200


# ---------------------------------------------------------------------------------
# robustness

def test_oversized_and_malformed_requests(env):
    c = env.client
    big = b"x=" + b"a" * (wp.MAX_BODY + 10)
    status, _, _ = c.request("POST", "/login", raw_body=big,
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 413
    assert c.request("POST", "/login", raw_body=b"\xff\xfe",
                     headers={"Content-Type": "application/x-www-form-urlencoded"})[0] in (400, 403)
    assert c.get("/nope")[0] == 404
    assert c.post("/", {"a": "b"})[0] == 405
    assert c.request("GET", "/", headers={"Cookie": 'wb_session="\\x00;;=='})[0] == 200
    assert c.get("/api/status")[0] == 200  # still alive


def test_handler_exception_renders_500_and_server_survives(env, monkeypatch):
    def boom(self):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(wp.Portal, "status", boom)
    status, _, html = env.client.get("/")
    assert status == 500 and "Internal error" in html and "kaboom" not in html
    monkeypatch.undo()
    assert env.client.get("/")[0] == 200


def test_sessions_are_capped():
    store = wp.SessionStore(cap=5)
    tokens = [store.create()[0] for _ in range(20)]
    assert len(store) == 5
    assert store.get(tokens[-1]) is not None and store.get(tokens[0]) is None


def test_portal_module_does_not_import_bacpypes3():
    import subprocess
    import sys
    code = ("import sys; import water_bacnet.portal; "
            "sys.exit(1 if any(m.startswith('bacpypes3') for m in sys.modules) else 0)")
    assert subprocess.run([sys.executable, "-c", code], cwd=ROOT).returncode == 0


# ---------------------------------------------------------------------------------
# the main service side: last-good fallback and the reload watcher

def test_main_falls_back_to_last_good(tmp_path, caplog):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[meter\n")
    (tmp_path / "config.toml.last-good").write_text("[meter]\nid = 42\n")
    c, loaded_from, sha, err = wmain.load_with_fallback(str(cfg))
    assert loaded_from == "last-good" and c.meter.id == 42 and err
    assert any(r.levelname == "ERROR" and "LAST-GOOD" in r.getMessage() for r in caplog.records)
    (tmp_path / "config.toml.last-good").unlink()
    with pytest.raises(ValueError):
        wmain.load_with_fallback(str(cfg))


def test_main_watcher_requests_reload_on_change(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    sup = wmain.Supervisor(cfg, "config", "x", runtime_dir=str(tmp_path), poll_s=0.05)

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(sup.watch(stop))
        await asyncio.sleep(0.2)
        assert not stop.is_set()
        cfg.write_text("[meter]\nid = 7\n")
        await asyncio.wait_for(stop.wait(), 5)
        await task

    asyncio.run(run())
    assert sup.reload_requested
    sup.write("running")
    d = json.loads((tmp_path / "main-status.json").read_text())
    assert d["state"] == "running" and d["loaded_from"] == "config"


def test_main_ready_only_when_bacnet_is_serving(tmp_path):
    sup = wmain.Supervisor(tmp_path / "c.toml", "config", "x", runtime_dir=str(tmp_path))
    sup.write("running")
    d = json.loads((tmp_path / "main-status.json").read_text())
    assert d["ready_at"] is None and d["ready_via"] == "signal"
    assert wp.main_ready_at(d, time.time() + 1000) is None  # no legacy fallback for us
    sup.mark_ready({"address": "192.0.2.5/24:47808"})  # bacnet.serve's on_ready hook
    d = json.loads((tmp_path / "main-status.json").read_text())
    assert d["ready_at"] and d["bacnet_address"] == "192.0.2.5/24:47808"
    assert wp.main_ready_at(d) == d["ready_at"]


def test_legacy_main_heartbeat_uses_80s_fallback():
    now = time.time()
    old = {"state": "running", "started": now - 10, "ready_at": None, "heartbeat": now}
    assert wp.main_ready_at(old, now) is None
    old["started"] = now - wp.LEGACY_READY_FALLBACK_S - 1
    assert wp.main_ready_at(old, now) is not None
    old["state"] = "failed"
    assert wp.main_ready_at(old, now) is None


def test_journal_only_for_logged_in_users(env, monkeypatch):
    calls = []

    def fake_journal(lines=50):
        calls.append(lines)
        return ["SECRET-LOG-LINE bind 198.51.100.99"], None
    monkeypatch.setattr(env.portal, "journal", fake_journal)
    c = env.client
    status, _, html = c.get("/")
    assert status == 200
    assert "SECRET-LOG-LINE" not in html and "log in" in html and not calls
    _, _, api = c.get("/api/status")
    assert "SECRET-LOG-LINE" not in api
    c.login()
    status, _, html = c.get("/")
    assert status == 200 and "SECRET-LOG-LINE" in html


def test_settings_form_has_new_sections_and_validates_them(env):
    c = env.client
    c.login()
    _, _, html = c.get("/settings")
    for name in ("recovery.power_cycle", "recovery.hub_location", "recovery.cycle_delay_s",
                 "recovery.max_reboots_per_week", "health.enclosure_sensor", "health.i2c_address",
                 "health.poll_s", "storage.raw_retention_days", "storage.save_interval_s"):
        assert f'name="{name}"' in html, name
    before = env.config.read_bytes()
    for field, value, needle in [("recovery.cycle_delay_s", "90", "cycle_delay_s"),
                                 ("recovery.hub_location", "1-1; reboot", "hub_location"),
                                 ("recovery.usb_vid_pid", "rtl", "usb_vid_pid"),
                                 ("recovery.max_power_cycles_per_day", "100", "max_power_cycles"),
                                 ("health.i2c_address", "0x50", "i2c_address"),
                                 ("health.poll_s", "inf", "number"),
                                 ("bacnet.refresh_s", "0.5", "refresh_s"),
                                 ("source.restart_if_silent_s", "120", "restart_if_silent_s"),
                                 ("storage.raw_retention_days", "0", "raw_retention_days")]:
        status, _, html = c.post("/settings", settings_form(c, **{field: value}))
        assert status == 400, field
        assert "Not saved" in html and needle in html, (field, html[:3000])
    assert env.config.read_bytes() == before
    with FakeMain(env.config, env.run):
        form = settings_form(c, **{"recovery.reboot": "on", "recovery.cycle_delay_s": "8",
                                   "health.enclosure_sensor": "bme280",
                                   "health.i2c_address": "0x77",
                                   "storage.raw_retention_days": "90"})
        status, _, _ = c.post("/settings", form)
        assert status == 303
        state = env.portal.wait_apply(10)
    assert state["state"] == "applied", state
    cfg = load_config(env.config)
    assert cfg.recovery.reboot is True and cfg.recovery.cycle_delay_s == 8
    assert cfg.health.enclosure_sensor == "bme280" and cfg.health.i2c_address == 0x77
    assert cfg.storage.raw_retention_days == 90
    _, _, html = c.get("/settings")
    assert 'value="0x77"' in html


def test_alive_but_not_ready_main_is_not_healthy(env):
    data = env.config.read_bytes()
    now = time.time()
    (env.run / "main-status.json").write_text(json.dumps({
        "state": "running", "loaded_from": "config", "started": now,
        "config_sha256": hashlib.sha256(data).hexdigest(), "ready_at": None,
        "heartbeat": now + 0.5}))
    ok, why = env.portal.wait_healthy(hashlib.sha256(data).hexdigest(), now, time.time() + 1.0)
    assert not ok and "did not report healthy" in why


# ---------------------------------------------------------------------------------
# [meter] id has no default: the service and --check refuse to run, the portal does not

def _no_id_config(env):
    text = env.config.read_text().replace(f"\nid = {METER_ID}\n", "\n")
    assert "\nid =" not in text
    env.config.write_text(text)
    return text


def test_portal_starts_and_sets_missing_meter_id(env):
    _no_id_config(env)
    _write_state(env)
    status, _, html = env.client.get("/")
    assert status == 200 and "Setup needed" in html and "0xFFFFFF" in html
    d = json.loads(env.client.get("/api/status")[2])
    assert d["config"]["loaded_from"] == "config"  # loads fine, just not usable yet
    assert d["config"]["meter_id"] == 0 and "rtl_433" in d["config"]["meter_id_problem"]
    assert d["feed"] == "stale" and "meter id not set" in d["reading"]["error"]
    c = env.client
    c.login()
    status, _, html = c.get("/settings")
    assert status == 200 and 'name="meter.id" value="0"' in html
    # Saving without an id is refused with a pointer to how to find it ...
    before = env.config.read_bytes()
    status, _, html = c.post("/settings", settings_form(c))
    assert status == 400 and "Not saved" in html and "[meter] id must be set" in html
    assert env.config.read_bytes() == before
    # ... and with one it is applied.
    with FakeMain(env.config, env.run):
        status, _, _ = c.post("/settings", settings_form(c, **{"meter.id": str(METER_ID)}))
        assert status == 303
        state = env.portal.wait_apply(10)
    assert state["state"] == "applied", state
    assert load_config(env.config).meter.id == METER_ID
    d = json.loads(env.client.get("/api/status")[2])
    assert d["config"]["meter_id_problem"] is None and d["feed"] == "fresh"


@pytest.mark.parametrize("meter_section", ["", "[meter]\nid = 0\n"])
def test_check_fails_clearly_without_meter_id(tmp_path, capsys, meter_section):
    cfg = tmp_path / "config.toml"
    cfg.write_text(meter_section + '[source]\nmode = "state_file"\n')
    assert wmain.main(["--config", str(cfg), "--check"]) == 2
    err = capsys.readouterr().err
    assert "[meter] id is not set" in err and "serial & 0xFFFFFF" in err
    assert "rtl_433" in err and "-R 223" in err


def test_service_refuses_to_start_without_meter_id(tmp_path, monkeypatch, caplog):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[source]\nmode = "state_file"\n')
    monkeypatch.setenv("WATER_BACNET_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("RUNTIME_DIRECTORY", raising=False)

    def no_serve(*a, **kw):
        raise AssertionError("must not start serving without a meter id")

    monkeypatch.setattr(wmain, "_serve", no_serve)
    assert wmain._run_service(str(cfg)) == 2
    assert any(r.levelname == "ERROR" and "serial & 0xFFFFFF" in r.getMessage()
               for r in caplog.records)
    hb = json.loads((tmp_path / "main-status.json").read_text())
    assert hb["state"] == "failed" and "[meter] id is not set" in hb["error"]
    assert hb["loaded_from"] == "config" and hb["ready_at"] is None
