"""Installed manager/preparer/CLI restart check; isolated user-systemd on Jah."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from robot_runtime.common import Fault
from robot_runtime.ipc import request
from integration_runtime import wait_for


def main():
    root = Path(sys.argv[1]).resolve()
    assert root.parent == Path("/tmp") and re.fullmatch(r"robot-runtime-slice-[0-9a-f]{12}", root.name)
    root.mkdir(mode=0o700, exist_ok=True)
    task = root / ("restart-" + uuid.uuid4().hex[:10])
    task.mkdir(mode=0o700)
    state, config, output = task / "state", task / "request.json", task / "ready.jsonl"
    config.write_text('{"threshold":0.5}')
    fixture = str(Path(__file__).with_name("prepared_fixture.py").resolve())
    profile = dict(schema_version=1, robot_id="fixture", namespace="r" + uuid.uuid4().hex[:10], units=[dict(
        id="worker", label="Prepared worker", argv=[sys.executable, fixture, "run", str(output)],
        preparation=dict(argv=[sys.executable, fixture, "prepare", str(config), str(task)], timeout_seconds=3),
        working_directory=str(task), environment={}, stop=dict(signal="SIGINT", timeout_seconds=2), public_logs=True)])
    profile_path = task / "profile.json"
    profile_path.write_text(json.dumps(profile))
    report = dict(checks=[], passed=False, cleanup_errors=[])
    log = (task / "manager.log").open("w")
    manager = subprocess.Popen([sys.executable, "-m", "robot_runtime.manager", "--profile", str(profile_path), "--state", str(state)], stdout=log, stderr=log)
    def unit():
        return request(state, "status", fresh=True)["units"][0]
    def cli(*argv):
        value = subprocess.run([sys.executable, "-m", "robot_runtime.cli", "--state", str(state), *argv],
                               capture_output=True, text=True, timeout=15, check=True)
        return json.loads(value.stdout)
    try:
        wait_for(lambda: request(state, "status")["fresh"])
        first = cli("start", "worker")
        wait_for(lambda: output.exists())
        assert json.loads(output.read_text().splitlines()[0])["threshold"] == .5
        original = unit()
        config.write_text('{"threshold":2}')
        try:
            cli("restart", "worker")
            raise AssertionError("invalid input restarted the unit")
        except subprocess.CalledProcessError as exc:
            assert json.loads(exc.stderr.splitlines()[-1])["error"]["code"] == "preparation_failed", exc.stderr
        assert unit()["run_id"] == first["run_id"] and unit()["main_pid"] == original["main_pid"]
        report["checks"].append("invalid_preparation_keeps_original_process")
        config.write_text('{"threshold":0.6}')
        operation = cli("restart", "worker")
        config.write_text('{"threshold":0.7}')
        result = wait_for(lambda: (op if (op := request(state, "operation", request_id=operation["request_id"]))["phase"] == "complete" else None))
        wait_for(lambda: len(output.read_text().splitlines()) == 2)
        rows = [json.loads(line) for line in output.read_text().splitlines()]
        assert rows[1]["threshold"] == .6 and rows[1]["run_id"] == result["new_run_id"]
        assert result["old_run_id"] == first["run_id"] and result["old_run_id"] != result["new_run_id"]
        report["checks"].append("replacement_uses_accepted_snapshot_and_records_both_runs")
        wait_for(lambda: cli("log", "worker")["records"])
        assert cli("log", "worker") == cli("logs", "worker")
        report["checks"].append("installed_log_aliases_are_equivalent")
        stop = cli("stop", "worker")
        wait_for(lambda: not unit()["blocked"])
        assert request(state, "operation", request_id=stop["request_id"])["phase"] == "complete"
        report["checks"].append("convenient_stop_preserves_exact_run_guard")
        report["passed"] = True
    finally:
        if manager.poll() is None:
            try:
                value = unit()
                if value["blocked"] and not value.get("stop_request_id"):
                    cli("stop", "worker", "--expected-run", value["run_id"])
                wait_for(lambda: not unit()["blocked"])
                report["final_status"] = unit()
            except Exception as exc:
                report["cleanup_errors"].append(repr(exc))
            manager.terminate()
            manager.wait(timeout=8)
        log.close()
        (task / "report.json").write_text(json.dumps(report, indent=2))
        if report["cleanup_errors"]:
            raise RuntimeError("Native restart check cleanup failed; inspect " + str(task))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
