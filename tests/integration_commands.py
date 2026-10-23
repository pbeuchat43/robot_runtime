"""Native single-publication commands, ownership checks and recovery on jah."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from command_fixture import make_profile
from robot_runtime.common import Fault
from robot_runtime.ipc import request
from integration_runtime import wait_for


def main(root):
    root = root.resolve()
    assert root.parent == Path("/tmp") and re.fullmatch(r"robot-runtime-ros-[0-9a-f]{12}", root.name)
    token = uuid.uuid4().hex[:10]
    area = root / ("commands-" + token)
    area.mkdir(mode=0o700)
    profile = make_profile(area, token)
    state = area / "state"
    log = (area / "manager.log").open("w")
    report, duplicate, manager = dict(checks=[], domain_id=228), None, None
    def launch():
        return subprocess.Popen([sys.executable, "-m", "robot_runtime.manager", "--profile", str(area / "profile.json"), "--state", str(state)], stdout=log, stderr=log)
    def local(action, **values): return request(state, action, **values)
    def status(): return local("status", fresh=True)
    def unit(): return status()["units"][0]
    def catalog(): return local("ros_status")
    def provider(): return json.loads((area / "provider-status.json").read_text())
    def check(name): report["checks"].append(name)
    def lifecycle(action, **values):
        return local(action, unit_id="idle", request_id=str(uuid.uuid4()), expires_at_ms=int(time.time()*1000)+10000,
                     config_revision=status()["config_revision"], **values)
    def command(**values):
        return dict(dict(capability_id="request", choice="enable", request_id=str(uuid.uuid4()), expires_at_ms=int(time.time()*1000)+10000,
            config_revision=status()["config_revision"], expected_run=unit()["run_id"], generation=catalog()["generation"]), **values)
    def complete(rid):
        return wait_for(lambda: (op if op["phase"] not in ("prepared", "in_progress") else None)
                        if (op := local("operation", request_id=rid)) else None)
    def fault(code, callback):
        try: callback()
        except Fault as exc: assert exc.code == code, (exc.code, code)
        else: raise AssertionError("Expected " + code)
    try:
        manager = launch()
        wait_for(lambda: status()["fresh"] and catalog()["available"], seconds=15)
        assert not catalog()["commands"][0]["available"]
        lifecycle("start")
        wait_for(lambda: catalog()["commands"][0]["available"], seconds=15)
        pid, run_id = unit()["main_pid"], unit()["run_id"]
        values = command()
        first = local("ros_topic_command", **values)
        assert local("ros_topic_command", **values)["request_id"] == first["request_id"]
        result = complete(first["request_id"])
        assert result["phase"] == "complete" and result["response"]["application_acceptance"] == "unconfirmed", result
        wait_for(lambda: provider()["received"] == [1] and provider()["command_publishers"] == 0)
        check("one_publication_deduplication_and_publisher_release")
        (area / "control.json").write_text(json.dumps(dict(ignore=True)))
        result = complete(local("ros_topic_command", **command(choice="disable"))["request_id"])
        wait_for(lambda: provider()["received"] == [1, 0])
        assert result["outcome"] == "published_once" and provider()["state"] == 1
        check("ignored_input_is_not_reported_as_application_acceptance")
        for changes, code in ((dict(choice="arbitrary"), "invalid_arguments"), (dict(expected_run="old"), "stale_run"),
                              (dict(generation="old"), "stale_generation"), (dict(expires_at_ms=0), "expired")):
            fault(code, lambda: local("ros_topic_command", **command(**changes)))
        spec = profile["units"][0]
        duplicate = subprocess.Popen(spec["argv"] + ["--duplicate"], env=spec["environment"], stdout=log, stderr=log)
        wait_for(lambda: catalog()["commands"][0]["error"] == "source_ambiguous", seconds=15)
        result = complete(local("ros_topic_command", **command())["request_id"])
        assert result["delivery"] == "not_sent" and provider()["received"] == [1, 0], result
        duplicate.terminate()
        duplicate.wait(timeout=8)
        duplicate = None
        wait_for(lambda: catalog()["commands"][0]["available"], seconds=15)
        check("arbitrary_stale_expired_and_duplicate_targets_refused")
        manager.kill()
        manager.wait(timeout=5)
        manager = launch()
        wait_for(lambda: status()["fresh"] and catalog()["commands"][0]["available"], seconds=15)
        assert unit()["main_pid"] == pid and unit()["run_id"] == run_id and provider()["received"] == [1, 0]
        assert local("ros_topic_command", **values)["request_id"] == first["request_id"]
        check("manager_restart_preserves_provider_and_does_not_replay")
        operation = json.loads(subprocess.check_output([sys.executable, "-m", "robot_runtime.cli", "--state", str(state), "ros", "command", "request", "disarm",
                                                        "--expected-run", run_id], text=True, timeout=12))
        assert complete(operation["request_id"])["phase"] == "complete"
        wait_for(lambda: provider()["received"] == [1, 0, 4] and provider()["command_publishers"] == 0)
        check("standalone_cli_without_web")
        report["passed"] = True
    except BaseException as exc:
        report.update(passed=False, error=repr(exc))
        raise
    finally:
        errors = []
        if duplicate and duplicate.poll() is None:
            duplicate.terminate()
            duplicate.wait(timeout=8)
        if manager and manager.poll() is None:
            try:
                if unit()["blocked"] and not unit().get("stop_request_id"):
                    lifecycle("stop", expected_run=unit()["run_id"])
                wait_for(lambda: not unit()["blocked"], seconds=10)
                report["final_status"] = status()
            except Exception as exc: errors.append(repr(exc))
            manager.terminate()
            manager.wait(timeout=10)
        log.close()
        report["cleanup_errors"] = errors
        (area / "report.json").write_text(json.dumps(report, indent=2))
        (root / "latest-commands.txt").write_text(area.name)
        if errors: raise RuntimeError("Inspect command cleanup report")
    print(json.dumps(dict(passed=report["passed"], checks=len(report["checks"]), path=str(area / "report.json"))))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    main(parser.parse_args().root)
