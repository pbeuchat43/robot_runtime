"""Finite preparation and harmless workload for the native restart check."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time
import uuid

if sys.argv[1] == "prepare":
    value = json.loads(Path(sys.argv[2]).read_bytes())
    if set(value) != {"threshold"} or type(value["threshold"]) not in (int, float) or not 0 <= value["threshold"] <= 1:
        raise SystemExit("threshold must be between zero and one")
    raw = json.dumps(value).encode()
    snapshot = Path(sys.argv[3]) / (uuid.uuid4().hex + ".json")
    snapshot.write_bytes(raw)
    print(json.dumps(dict(schema_version=1, unit_id=os.environ["ROBOT_RUNTIME_PREPARE_UNIT"],
        config_revision=os.environ["ROBOT_RUNTIME_PREPARE_REVISION"],
        mode=os.environ["ROBOT_RUNTIME_PREPARE_MODE"] or None,
        inputs=[dict(name="configuration", path=str(snapshot), sha256=hashlib.sha256(raw).hexdigest())])))
else:
    from robot_runtime.prepared import run_inputs
    _manifest, inputs = run_inputs()
    record = dict(json.loads(inputs["configuration"]), run_id=os.environ["ROBOT_RUNTIME_RUN"])
    with Path(sys.argv[2]).open("a") as stream:
        stream.write(json.dumps(record) + "\n")
    print(json.dumps(record), flush=True)
    stopped = False
    def stop(_signal, _frame):
        global stopped
        stopped = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    deadline = time.monotonic() + 60
    while not stopped and time.monotonic() < deadline:
        time.sleep(.02)
