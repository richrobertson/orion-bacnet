#!/usr/bin/env python3
"""Stand-in for rtl_433 used by the test suite. Stdlib only.

Accepts and ignores any rtl_433 arguments, then replays a JSONL fixture to stdout
the way `rtl_433 -F json` would, interleaving the noise a real receiver produces:
garbage/non-JSON lines, a neighbour's ORION meter (id 1234567), and a different
model that happens to share our id.

Environment knobs:
  FAKE_RTL_FIXTURE     JSONL to replay (default: fixtures/packets.jsonl next to this file;
                       an empty file or /dev/null = never print anything)
  FAKE_RTL_DELAY       seconds between output lines (default 0.05)
  FAKE_RTL_EXIT_AFTER  exit with status 1 after this many OUR-meter packets
  FAKE_RTL_NOISE       "0" disables garbage/neighbour/wrong-model lines (default "1")
  FAKE_RTL_ARGV_LOG    append one JSON line per invocation: {"pid":..,"argv":[..]}
  FAKE_RTL_ON_END      what to do after the fixture is exhausted: "hang" (default, like a
                       real rtl_433 waiting for more packets) or "exit"

`-V` prints a version banner to stderr and exits 0, like rtl_433.
"""

import json
import os
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUR_ID = 7654321


def emit(line):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main(argv):
    log = os.environ.get("FAKE_RTL_ARGV_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(json.dumps({"pid": os.getpid(), "argv": argv}) + "\n")

    if "-V" in argv:
        sys.stderr.write("rtl_433 version 25.02 (fake for tests)\n")
        return 0

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    fixture = os.environ.get("FAKE_RTL_FIXTURE", os.path.join(HERE, "fixtures", "packets.jsonl"))
    delay = float(os.environ.get("FAKE_RTL_DELAY", "0.05"))
    exit_after = os.environ.get("FAKE_RTL_EXIT_AFTER")
    exit_after = int(exit_after) if exit_after else None
    noise = os.environ.get("FAKE_RTL_NOISE", "1") != "0"
    on_end = os.environ.get("FAKE_RTL_ON_END", "hang")

    sys.stderr.write("rtl_433 version 25.02 (fake)\nRegistered 1 out of 300 device decoding protocols [ 223 ]\n")
    sys.stderr.flush()

    with open(fixture) as fh:
        packets = [ln.strip() for ln in fh if ln.strip()]

    ours = 0
    for i, line in enumerate(packets):
        time.sleep(delay)
        if noise and i == 1:
            emit("garbage line that is not json {")
            emit("")
            emit('{"time":"2026-10-18T10:01:00","model":"Badger-ORION","id":1234567,'
                 '"volume_gal":5,"mic":"CRC","snr":9.1,"rssi":-20.0}')
            emit('{"time":"2026-10-18T10:01:05","model":"Neptune-R900","id":%d,'
                 '"consumption":999999,"mic":"CRC"}' % OUR_ID)
            emit("[1, 2, 3]")
        emit(line)
        try:
            if json.loads(line).get("id") == OUR_ID:
                ours += 1
        except ValueError:
            pass
        if exit_after is not None and ours >= exit_after:
            sys.stderr.write("fake rtl_433: exiting early as asked\n")
            return 1

    if on_end == "exit":
        return 0
    while True:  # a real rtl_433 just sits there waiting for the next packet
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
