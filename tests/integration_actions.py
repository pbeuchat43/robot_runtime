"""Finite native Jazzy actions, restart recovery and standalone CLI on jah."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from robot_runtime.common import Fault
from robot_runtime.ipc import request
from action_fixture import make_profile
from integration_runtime import wait_for


def main(root):
    root = root.resolve()
    assert root.parent == Path("/tmp") and re.fullmatch(r"robot-runtime-ros-[0-9a-f]{12}", root.name)
    token = uuid.uuid4().hex[:10]
    area = root / ("actions-" + token)
    area.mkdir(mode=0o700)
    profile = make_profile(area, token)
    state = area / "state"
    log = (area / "manager.log").open("w")
    report = dict(checks=[], domain_id=227, source_sha256={str(p.relative_to(Path(__file__).resolve().parent.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path(__file__).resolve().parent.parent.joinpath("src").rglob("*") if p.is_file() and "__pycache__" not in p.parts})
    manager, duplicate = None, None

    def launch():
        return subprocess.Popen([sys.executable, "-m", "robot_runtime.manager", "--profile", str(area / "profile.json"), "--state", str(state)], stdout=log, stderr=log)
    def local(action, **values):
        return request(state, action, **values)
    def status():
        return local("status", fresh=True)
    def unit():
        return status()["units"][0]
    def catalog():
        return local("ros_actions")
    def action():
        return catalog()["actions"][0]
    def provider():
        return json.loads((area / "provider-status.json").read_text())
    def control(**values):
        temporary = area / "control.tmp"
        temporary.write_text(json.dumps(values))
        os.replace(temporary, area / "control.json")
    def check(name, **values):
        report["checks"].append(dict(check=name, passed=True, **values))
    def lifecycle(command, **values):
        return local(command, unit_id="idle", request_id=str(uuid.uuid4()), expires_at_ms=int(time.time()*1000)+10000,
                     config_revision=status()["config_revision"], **values)
    def command(**changes):
        value = dict(capability_id="count", request_id=str(uuid.uuid4()), expires_at_ms=int(time.time()*1000)+10000,
                     config_revision=status()["config_revision"], expected_run=unit()["run_id"], generation=catalog()["generation"],
                     arguments=dict(steps=20, period_seconds=.1, abort_at_end=False))
        value.update(changes)
        return value
    def goal(**changes):
        return local("ros_action_goal", **command(**changes))
    def operation(rid):
        return local("operation", request_id=rid)
    def terminal(rid, seconds=15):
        return wait_for(lambda: operation(rid) if operation(rid).get("terminal") else None, seconds=seconds)
    def cancel(rid):
        values = command()
        values.pop("arguments")
        return local("ros_action_cancel", **values, goal_request_id=rid)
    def fault(code, callback):
        try:
            callback()
            raise AssertionError("Expected " + code)
        except Fault as exc:
            assert exc.code == code, (exc.code, code)
    def cli(*args):
        return json.loads(subprocess.check_output([sys.executable, "-m", "robot_runtime.cli", "--state", str(state), "ros", *args], text=True, timeout=12))

    try:
        manager = launch()
        wait_for(lambda: status()["fresh"] and catalog()["available"], seconds=15)
        assert not action()["available"] and unit()["lifecycle"] == "stopped"
        lifecycle("start")
        wait_for(lambda: action()["available"], seconds=15)
        pid, run_id = unit()["main_pid"], unit()["run_id"]
        values = command()
        first = local("ros_action_goal", **values)
        assert local("ros_action_goal", **values)["request_id"] == first["request_id"]
        fault("busy", lambda: goal())
        wait_for(lambda: action()["feedback_fresh"])
        result = terminal(first["request_id"])
        assert result["goal_status"] == "succeeded" and result["result"]["completed"] == 20, result
        assert provider()["goals_received"] == provider()["goals_accepted"] == 1
        assert provider()["goal_ids"] == [uuid.UUID(first["request_id"]).hex]
        check("native_uuid_goal_feedback_result_capacity_and_deduplication", goal=result)
        control(reject_goals=True)
        rejected = terminal(goal()["request_id"])
        assert rejected["goal_status"] == "rejected" and rejected["accepted"] is False, rejected
        control()
        aborted = terminal(goal(arguments=dict(steps=2, period_seconds=.05, abort_at_end=True))["request_id"])
        assert aborted["goal_status"] == "aborted" and not aborted["result"]["success"]
        check("provider_goal_rejection_and_terminal_abort_are_distinct")
        control(cancel_delay=2)
        current = goal(arguments=dict(steps=80, period_seconds=.1, abort_at_end=False))
        wait_for(lambda: action()["feedback_fresh"])
        requested = cancel(current["request_id"])
        wait_for(lambda: operation(requested["request_id"]).get("cancel_accepted") is True)
        assert not operation(current["request_id"])["terminal"]
        fault("busy", lambda: goal())
        assert terminal(current["request_id"])["goal_status"] == "canceled"
        check("cancel_acknowledgement_precedes_terminal_cancellation")
        control(reject_cancel=True)
        current = goal()
        wait_for(lambda: operation(current["request_id"]).get("accepted"))
        requested = cancel(current["request_id"])
        wait_for(lambda: operation(requested["request_id"]).get("cancel_accepted") is False)
        assert terminal(current["request_id"])["goal_status"] == "succeeded"
        check("provider_cancel_refusal_leaves_goal_running")
        control(accept_delay=2.5)
        before = provider()["goals_received"]
        current = goal()
        uncertain = wait_for(lambda: operation(current["request_id"]) if operation(current["request_id"])["phase"] in ("timed_out", "outcome_unknown") else None)
        assert uncertain.get("accepted") is not True
        fault("busy", lambda: goal())
        assert terminal(current["request_id"])["goal_status"] == "succeeded"
        assert provider()["goals_received"] == before+1
        check("late_acceptance_resolves_timeout_by_uuid_without_resend", uncertain=uncertain)
        control()
        current = goal(arguments=dict(steps=100, period_seconds=.1, abort_at_end=False))
        wait_for(lambda: action()["feedback_fresh"])
        control(pause_feedback=True)
        wait_for(lambda: not action()["feedback_fresh"])
        assert not operation(current["request_id"])["terminal"]
        control()
        wait_for(lambda: action()["feedback_fresh"])
        generation = catalog()["generation"]
        workers = []
        for path in Path("/proc").glob("[0-9]*/status"):
            try:
                parent = next(line.split()[1] for line in path.read_text().splitlines() if line.startswith("PPid:"))
                if int(parent) == manager.pid and b"ros_worker.py" in (path.parent / "cmdline").read_bytes():
                    workers.append(int(path.parent.name))
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                pass
        assert len(workers) == 1
        before = provider()["goals_received"]
        os.kill(workers[0], signal.SIGKILL)
        wait_for(lambda: catalog()["available"] and catalog()["generation"] != generation, seconds=15)
        assert terminal(current["request_id"], seconds=15)["goal_status"] == "succeeded"
        assert provider()["goals_received"] == before and unit()["main_pid"] == pid
        check("feedback_expiry_and_worker_crash_recover_original_goal_without_replay")
        current = goal(arguments=dict(steps=100, period_seconds=.1, abort_at_end=False))
        wait_for(lambda: action()["feedback_fresh"])
        before = provider()["goals_received"]
        manager.kill()
        manager.wait(timeout=5)
        manager = launch()
        wait_for(lambda: status()["fresh"] and action()["available"], seconds=15)
        assert unit()["main_pid"] == pid and unit()["run_id"] == run_id
        assert terminal(current["request_id"], seconds=15)["goal_status"] == "succeeded"
        assert provider()["goals_received"] == before
        check("manager_crash_recovers_result_without_provider_restart_or_goal_replay")
        for changes, code in ((dict(expected_run="old"), "stale_run"), (dict(generation="old"), "stale_generation"),
                              (dict(arguments=dict(steps=True, period_seconds=.1, abort_at_end=False)), "invalid_arguments")):
            fault(code, lambda changes=changes: goal(**changes))
        spec = profile["units"][0]
        duplicate = subprocess.Popen(spec["argv"] + ["--duplicate"], env=spec["environment"], stdout=log, stderr=log)
        wait_for(lambda: action()["error"] == "source_ambiguous", seconds=15)
        before = provider()["goals_received"]
        denied = terminal(goal()["request_id"])
        assert denied["delivery"] == "not_sent" and provider()["goals_received"] == before
        duplicate.terminate()
        duplicate.wait(timeout=8)
        duplicate = None
        wait_for(lambda: action()["available"], seconds=15)
        check("stale_targets_and_ambiguous_native_provider_block_dispatch")
        assert cli("actions")["actions"][0]["id"] == "count"
        expired = subprocess.run([sys.executable, "-m", "robot_runtime.cli", "--state", str(state), "ros", "action", "send", "count",
            "--expected-run", run_id, "--expires-at-ms", "0", "--arguments", json.dumps(dict(steps=2, period_seconds=.1, abort_at_end=False))],
            capture_output=True, text=True, timeout=12)
        assert expired.returncode == 1 and "Request ID:" in expired.stderr and '"code": "expired"' in expired.stderr
        current = cli("action", "send", "count", "--expected-run", run_id, "--arguments", json.dumps(dict(steps=50, period_seconds=.1, abort_at_end=False)))
        wait_for(lambda: operation(current["request_id"]).get("accepted"))
        cli("action", "cancel", "count", "--expected-run", run_id, "--goal", current["request_id"])
        assert terminal(current["request_id"])["goal_status"] == "canceled"
        check("standalone_cli_lists_sends_and_cancels_without_web")
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
            except Exception as exc:
                errors.append(repr(exc))
            manager.terminate()
            manager.wait(timeout=10)
        log.close()
        report["cleanup_errors"] = errors
        (area / "report.json").write_text(json.dumps(report, indent=2))
        (root / "latest-actions.txt").write_text(area.name)
        if errors:
            raise RuntimeError("Inspect action cleanup report")
    print(json.dumps(dict(passed=report["passed"], checks=len(report["checks"]), path=str(area / "report.json"))))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    main(parser.parse_args().root)
