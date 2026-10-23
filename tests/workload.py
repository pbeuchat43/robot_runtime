"""Finite software fixture, never a hardware provider."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import json
import os
import signal
import sys
import time
from pathlib import Path

mode, filename = sys.argv[1:]
events = Path(filename)
deadline = None


def record(event, **values):
    value = dict(event=event, pid=os.getpid(), monotonic=time.monotonic(), **values)
    with events.open("a") as stream:
        stream.write(json.dumps(value) + "\n")
    print(json.dumps(value), flush=True)


def stop(number, _frame):
    global deadline
    record("signal", signal=number)
    if mode == "slow":
        deadline = deadline or time.monotonic() + 3
    else:
        record("exit")
        raise SystemExit(0)


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
signal.alarm(900 if mode == "benchmark" else 180)
if mode == "descendant":
    child = os.fork()
    if child:
        record("parent_exit", child=child)
        raise SystemExit(0)
    os.setsid()
record("ready", environment_marker=os.environ.get("ROBOT_RUNTIME_RUN"), inherited_probe=os.environ.get("SHOULD_NOT_LEAK"))
if mode in ("success", "failure"):
    raise SystemExit(0 if mode == "success" else 7)
while True:
    if deadline and time.monotonic() >= deadline:
        record("exit")
        raise SystemExit(0)
    time.sleep(30 if mode == "benchmark" else 0.05)
