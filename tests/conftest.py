"""Shared test helpers. No pytest plugins required (no pytest-asyncio)."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
FIXTURES = TESTS / "fixtures"
FAKE_RTL = TESTS / "fake_rtl_433.py"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

METER_ID = 7654321
GAL_PER_CUFT = 7.480519


def load_packets():
    return [json.loads(ln) for ln in (FIXTURES / "packets.jsonl").read_text().splitlines() if ln.strip()]


def write_config(path: Path, text: str):
    from water_bacnet.config import load_config

    path.write_text(text)
    return load_config(path)


@pytest.fixture
def fake_rtl(tmp_path):
    """An executable wrapper that runs fake_rtl_433.py under this interpreter."""
    wrapper = tmp_path / "rtl_433"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_RTL}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return wrapper


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # reap if it is our zombie child
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return False
    except ChildProcessError:
        pass
    return True
