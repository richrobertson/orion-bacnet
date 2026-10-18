import ast
import dataclasses

import pytest

from conftest import ROOT
from water_bacnet.config import load_config

DEFAULTS = {
    ("meter", "id"): 0,  # no default: must be set (meter_id_problem)
    ("meter", "stale_after_s"): 900,
    ("source", "mode"): "rtl_433",
    ("source", "state_file"): "/var/lib/water-meter/state.json",
    ("source", "rtl433_path"): "rtl_433",
    ("source", "device"): "",  # first dongle
    ("source", "frequency"): "916.45M",
    ("source", "sample_rate"): "1600k",
    ("source", "protocol"): 223,
    ("source", "restart_if_silent_s"): 1800,
    ("storage", "state_dir"): "/var/lib/water-bacnet",
    ("storage", "raw_log"): True,
    ("bacnet", "bind"): "auto",
    ("bacnet", "port"): 47808,
    ("bacnet", "device_instance"): 2801,
    ("bacnet", "device_name"): "WaterMeter",
    ("bacnet", "vendor_id"): 999,
    ("bacnet", "refresh_s"): 15,
}


def _check_defaults(cfg, skip=()):
    for (sec, key), want in DEFAULTS.items():
        if (sec, key) in skip:
            continue
        assert getattr(getattr(cfg, sec), key) == want, (sec, key)


def test_empty_file_gives_all_defaults(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("")
    _check_defaults(load_config(p))


def test_partial_file_fills_the_rest(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[source]\nmode = "state_file"\n[bacnet]\nport = 47809\n')
    cfg = load_config(p)
    assert cfg.source.mode == "state_file"
    assert cfg.bacnet.port == 47809
    _check_defaults(cfg, skip={("source", "mode"), ("bacnet", "port")})


def test_config_is_frozen(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("")
    cfg = load_config(p)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.bacnet.port = 1


def test_bad_mode_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[source]\nmode = "sdr"\n')
    with pytest.raises(ValueError):
        load_config(p)


def test_example_config_parses():
    ex = ROOT / "config.example.toml"
    assert ex.exists(), "config.example.toml missing from repo root"
    cfg = load_config(ex)
    assert cfg.meter.id == 0  # placeholder: the user must set it
    assert cfg.bacnet.device_instance == 2801
    assert cfg.source.mode in ("rtl_433", "state_file")
    assert cfg.bacnet.bind != "0.0.0.0"


def test_python311_syntax_guard():
    """Pi OS Bookworm ships 3.11: no 3.12+ syntax (type aliases, PEP 695 generics)."""
    files = sorted((ROOT / "water_bacnet").glob("*.py"))
    assert files
    for f in files:
        src = f.read_text()
        tree = ast.parse(src, filename=str(f), feature_version=(3, 11))
        for node in ast.walk(tree):
            assert type(node).__name__ not in ("TypeAlias", "TypeVar", "ParamSpec", "TypeVarTuple"), \
                f"{f.name}:{getattr(node, 'lineno', '?')} uses 3.12+ syntax"
            if hasattr(node, "type_params"):
                assert not node.type_params, f"{f.name}:{node.lineno} uses PEP 695 generics"


# ---------------------------------------------------------------------------------
# [storage] additions, [recovery], [health] and load-time validation

NEW_DEFAULTS = {
    ("storage", "raw_retention_days"): 365,
    ("storage", "save_interval_s"): 900,
    ("recovery", "usb_reset"): True,
    ("recovery", "power_cycle"): True,
    ("recovery", "uhubctl_path"): "uhubctl",
    ("recovery", "hub_location"): "1-1",
    ("recovery", "hub_port"): "",
    ("recovery", "cycle_delay_s"): 5,
    ("recovery", "reboot"): False,
    ("recovery", "usb_vid_pid"): "0bda:2838",
    ("recovery", "max_power_cycles_per_day"): 4,
    ("recovery", "max_reboots_per_week"): 1,
    ("recovery", "step_timeout_s"): 120,
    ("health", "cpu_temp"): True,
    ("health", "thermal_path"): "/sys/class/thermal/thermal_zone0/temp",
    ("health", "throttled"): True,
    ("health", "vcgencmd_path"): "vcgencmd",
    ("health", "enclosure_sensor"): "auto",
    ("health", "i2c_bus"): 1,
    ("health", "i2c_address"): 0,
    ("health", "poll_s"): 60,
}


def test_new_keys_have_documented_defaults():
    cfg = load_config(None)
    for (sec, key), want in NEW_DEFAULTS.items():
        assert getattr(getattr(cfg, sec), key) == want, (sec, key)


def test_defaults_match_radio_side_table():
    """sources.cfg_get / the root helper fall back to usbctl.DEFAULTS: same values+types."""
    from water_bacnet import usbctl
    cfg = load_config(None)
    for (sec, key), want in usbctl.DEFAULTS.items():
        got = getattr(getattr(cfg, sec), key)
        assert got == want and type(got) is type(want), (sec, key, got, want)


def test_example_config_lists_every_key():
    import dataclasses as dc
    import tomllib
    data = tomllib.loads((ROOT / "config.example.toml").read_text())
    cfg = load_config(None)
    for sec in ("meter", "source", "storage", "bacnet", "portal", "recovery", "health"):
        for f in dc.fields(getattr(cfg, sec)):
            if f.name == "object_names":
                continue
            assert f.name in data.get(sec, {}), f"config.example.toml lacks [{sec}] {f.name}"
    assert load_config(ROOT / "config.example.toml") == dc.replace(cfg, path=str(ROOT / "config.example.toml"))


@pytest.mark.parametrize("text, needle", [
    ("[meter]\nstale_after_s = 30\n", "stale_after_s"),
    ("[meter]\nstale_after_s = 90000\n", "stale_after_s"),
    ("[meter]\nstale_after_s = nan\n", "finite"),
    ("[meter]\nstale_after_s = inf\n", "finite"),
    ("[source]\nrestart_if_silent_s = 299\n", "restart_if_silent_s"),
    ("[bacnet]\nrefresh_s = 0.5\n", "refresh_s"),
    ("[bacnet]\nrefresh_s = 301\n", "refresh_s"),
    ("[bacnet]\nvendor_id = 70000\n", "vendor_id"),
    ("[bacnet]\nvendor_id = -1\n", "vendor_id"),
    ('[bacnet]\nbind = "192.0.2.300/24"\n', "bind"),
    ('[bacnet]\nbind = "eth0"\n', "bind"),
    ('[bacnet]\nbind = "0.0.0.0/0"\n', "bind"),
    ('[bacnet]\nbind = "192.0.2.255/24"\n', "bind"),
    ("[recovery]\ncycle_delay_s = 0\n", "cycle_delay_s"),
    ("[recovery]\ncycle_delay_s = 61\n", "cycle_delay_s"),
    ("[recovery]\ncycle_delay_s = 2.5\n", "integer"),
    ("[recovery]\nstep_timeout_s = inf\n", "finite"),
    ('[recovery]\nreboot = "yes"\n', "true/false"),
])
def test_load_time_validation(tmp_path, text, needle):
    p = tmp_path / "c.toml"
    p.write_text(text)
    with pytest.raises(ValueError, match=needle):
        load_config(p)


def test_load_time_accepts_edges(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[meter]\nstale_after_s = 60\n[source]\nrestart_if_silent_s = 300\n'
                 '[bacnet]\nrefresh_s = 1\nvendor_id = 0\nbind = "192.0.2.10"\n'
                 '[recovery]\ncycle_delay_s = 60\n')
    cfg = load_config(p)
    assert cfg.bacnet.bind == "192.0.2.10" and cfg.recovery.cycle_delay_s == 60


def test_bad_health_value_falls_back_to_default(tmp_path, caplog):
    p = tmp_path / "c.toml"
    p.write_text('[health]\npoll_s = "often"\ni2c_bus = 3\n')
    cfg = load_config(p)
    assert cfg.health.poll_s == 60.0 and cfg.health.i2c_bus == 3
    p.write_text("[health]\npoll_s = inf\n")
    assert load_config(p).health.poll_s == 60.0  # lenient section: default, not a crash


def test_portal_problems_cover_new_sections():
    from water_bacnet.config import loads_config, problems
    cfg = loads_config('[recovery]\nhub_location = "1-1 -a off"\nusb_vid_pid = "bad"\n'
                       'max_power_cycles_per_day = 49\nmax_reboots_per_week = 8\n'
                       'uhubctl_path = "-x"\nhub_port = "a"\n'
                       '[health]\nenclosure_sensor = "dht22"\ni2c_address = 0x50\npoll_s = 1\n'
                       'thermal_path = "relative"\n'
                       '[storage]\nraw_retention_days = 0\nsave_interval_s = 1\n')
    text = " | ".join(problems(cfg))
    for needle in ("hub_location", "usb_vid_pid", "max_power_cycles_per_day",
                   "max_reboots_per_week", "uhubctl_path", "hub_port", "enclosure_sensor",
                   "i2c_address", "poll_s", "thermal_path", "raw_retention_days",
                   "save_interval_s"):
        assert needle in text, needle
    # The built-in defaults are complete except for the meter id, which has no default.
    found = problems(load_config(None))
    assert len(found) == 1 and "[meter] id must be set" in found[0] and "0xFFFFFF" in found[0]
    assert problems(loads_config("[meter]\nid = 7654321\n")) == []
    # explicit bind without a prefix loads, but the portal insists on one
    assert any("bind" in x for x in problems(loads_config('[bacnet]\nbind = "192.0.2.3"\n')))
