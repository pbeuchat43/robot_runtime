"""Finite real-container ownership checks on an already reserved Linux host."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from robot_runtime.ipc import request
from integration_runtime import wait_for


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(mode=0o700)  # Require a new private fixture directory.
    state = root / "state"
    token = "d" + uuid.uuid4().hex[:10]
    normal = "import signal,time,sys; signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); time.sleep(120)"
    slow = "import signal,time; signal.signal(signal.SIGTERM, lambda *_: time.sleep(4)); time.sleep(5)"
    units = []
    for name, program in (("normal", normal), ("slow", slow)):
        units.append(dict(id=name, label=name, argv=["/bin/sleep", "120"], environment={}, working_directory=str(root),
            stop=dict(signal="SIGTERM", timeout_seconds=3), preparation=dict(argv=["/bin/false"], timeout_seconds=1),
            docker=dict(image=args.image, argv=["/usr/bin/python3", "-c", program], environment={}, mounts=[], gpu=False, prepared_input="effective")))
    profile = root / "profile.json"
    profile.write_text(json.dumps(dict(schema_version=1, namespace=token, robot_id="fixture", units=units)))
    config = root / "effective"
    config.write_bytes(b"{}")
    log = (root / "manager.log").open("w")
    report = dict(checks=[], cleanup_errors=[])
    manager = None

    def launch():
        return subprocess.Popen([sys.executable, "-m", "robot_runtime.manager", "--profile", str(profile), "--state", str(state)], stdout=log, stderr=log)

    def status():
        return request(state, "status", fresh=True)

    def unit(name="normal"):
        return next(item for item in status()["units"] if item["unit_id"] == name)

    def command(action, name="normal", **values):
        revision = status()["config_revision"]
        if action == "start":
            values["prepared"] = dict(schema_version=1, unit_id=name, config_revision=revision, mode=None,
                inputs=[dict(name="effective", path=str(config), sha256=hashlib.sha256(b"{}").hexdigest())])
        return request(state, action, unit_id=name, request_id=str(uuid.uuid4()), expires_at_ms=int(time.time()*1000)+10000,
                       config_revision=revision, **values)

    def running(name="normal"):
        return wait_for(lambda: unit(name) if unit(name).get("container", {}).get("state") == "live" else None, seconds=15)

    def check(name):
        report["checks"].append(dict(check=name, passed=True))

    try:
        manager = launch()
        wait_for(lambda: status()["fresh"])
        command("start")
        original = running()
        manager.kill()
        manager.wait(5)
        manager = launch()
        wait_for(lambda: status()["fresh"])
        recovered = running()
        assert recovered["run_id"] == original["run_id"] and recovered["container"]["id"] == original["container"]["id"]
        command("stop", expected_run=original["run_id"])
        wait_for(lambda: not unit()["blocked"], seconds=15)
        check("manager_crash_recovers_exact_container_and_stop_removes_it")

        started = command("start")
        command("stop", expected_run=started["run_id"])
        wait_for(lambda: not unit()["blocked"], seconds=15)
        check("immediate_stop_during_start_cleans_both_resources")

        command("start")
        current = running()
        details = request(state, "inspect", run_id=current["run_id"])
        subprocess.run(["systemctl", "--user", "kill", "--signal=SIGTERM", details["name"]], check=True)
        wait_for(lambda: not unit()["blocked"], seconds=15)
        check("native_launcher_death_cleans_owned_container")

        command("start", "slow")
        current = running("slow")
        time.sleep(.3)  # Let the finite fixture install its handler.
        operation = command("stop", "slow", expected_run=current["run_id"])
        wait_for(lambda: request(state, "operation", request_id=operation["request_id"])["phase"] == "timed_out", seconds=8)
        assert unit("slow")["blocked"]
        wait_for(lambda: not unit("slow")["blocked"], seconds=15)
        assert request(state, "operation", request_id=operation["request_id"])["phase"] == "timed_out"
        check("graceful_stop_deadline_never_forces_exit_or_rewrites_timeout")
        report["passed"] = True
    except BaseException as exc:
        report.update(passed=False, error=repr(exc))
        raise
    finally:
        if manager and manager.poll() is None:
            try:
                for item in status()["units"]:
                    if item["blocked"] and not item.get("stop_request_id"):
                        command("stop", item["unit_id"], expected_run=item["run_id"])
                wait_for(lambda: all(not item["blocked"] for item in status()["units"]), seconds=20)
                report["final_status"] = status()
                names = subprocess.check_output(["docker", "ps", "-a", "--format", "{{.Names}}"], text=True).splitlines()
                assert not any(name.startswith("rr-" + token + "-") for name in names)
            except Exception as exc:
                report["cleanup_errors"].append(repr(exc))
            manager.terminate()
            manager.wait(10)
        log.close()
        (root / "result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return int(not report.get("passed") or bool(report["cleanup_errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
