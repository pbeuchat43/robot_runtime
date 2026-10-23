"""Live isolated user-systemd/CLI checks. Invoke only through ssh jah."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from robot_runtime.common import Fault
from robot_runtime.ipc import request


def wait_for(function, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = function()
            if value:
                return value
        except (Fault, FileNotFoundError, ConnectionRefusedError):
            pass
        time.sleep(0.05)
    raise AssertionError("condition timed out")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    assert root.parent == Path("/tmp") and re.fullmatch(r"robot-runtime-slice-[0-9a-f]{12}", root.name)
    state = root / ("state-" + uuid.uuid4().hex[:8])
    token = uuid.uuid4().hex[:10]
    fixture = Path(__file__).with_name("workload.py").resolve()
    profile = dict(schema_version=1, robot_id="fixture", namespace="t" + token, units=[])
    for mode in ("idle", "success", "failure", "slow", "descendant"):
        profile["units"].append(dict(id=mode, label=mode.title(), argv=[sys.executable, str(fixture), mode, str(root / (token + "-" + mode + ".jsonl"))],
                                     working_directory=str(root), environment={}, stop=dict(signal="SIGINT", timeout_seconds=0.3 if mode == "slow" else 2), public_logs=True))
    profile_path = root / (token + "-profile.json")
    profile["units"][0]["conflicts"] = ["peer"]
    profile["units"].append(dict(profile["units"][0], id="peer", label="Peer", conflicts=["idle"]))
    profile_path.write_text(json.dumps(profile))
    log = (root / (token + "-manager.log")).open("w")
    report = dict(token=token, checks=[], state=str(state), profile=str(profile_path),
                  source_sha256={str(p.relative_to(fixture.parent.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted((fixture.parent.parent / "src").rglob("*")) if p.is_file() and "__pycache__" not in p.parts})
    manager = None

    def launch():
        env = dict(os.environ, SHOULD_NOT_LEAK="manager-test-value")
        return subprocess.Popen([sys.executable, "-m", "robot_runtime.manager", "--profile", str(profile_path), "--state", str(state)], stdout=log, stderr=log, env=env)

    def status():
        return request(state, "status", fresh=True)

    def unit(name):
        return next(value for value in status()["units"] if value["unit_id"] == name)

    def command(action, name, **values):
        return request(state, action, unit_id=name, request_id=str(uuid.uuid4()), expires_at_ms=int(time.time() * 1000) + 10000,
                       config_revision=status()["config_revision"], **values)

    def check(name, **values):
        report["checks"].append(dict(check=name, passed=True, **values))

    try:
        manager = launch()
        wait_for(lambda: request(state, "status")["fresh"])
        assert all(value["lifecycle"] == "stopped" for value in status()["units"])
        check("startup_does_not_launch")
        cli = subprocess.run([sys.executable, "-m", "robot_runtime.cli", "--state", str(state), "start", "idle"], capture_output=True, text=True, timeout=10, check=True)
        started = json.loads(cli.stdout)
        original = wait_for(lambda: unit("idle") if unit("idle")["lifecycle"] == "running" else None)
        wait_for(lambda: (root / (token + "-idle.jsonl")).exists())
        ready = json.loads((root / (token + "-idle.jsonl")).read_text().splitlines()[0])
        assert ready["inherited_probe"] is None and ready["environment_marker"] == started["run_id"]
        check("cli_start_and_explicit_environment", run_id=started["run_id"])
        manager.kill()
        manager.wait(timeout=5)
        manager = launch()
        wait_for(lambda: request(state, "status")["fresh"])
        recovered = unit("idle")
        assert recovered["run_id"] == original["run_id"] and recovered["main_pid"] == original["main_pid"]
        check("abrupt_manager_restart_preserves_workload", pid=recovered["main_pid"])
        try:
            command("stop", "idle", expected_run="stale")
            raise AssertionError("stale stop accepted")
        except Fault as exc:
            assert exc.code == "stale_run"
        stopped = command("stop", "idle", expected_run=original["run_id"])
        wait_for(lambda: not unit("idle")["blocked"])
        assert request(state, "operation", request_id=stopped["request_id"])["phase"] == "complete"
        check("stale_stop_rejected_and_graceful_stop_completes")
        logs = wait_for(lambda: request(state, "logs", run_id=original["run_id"], limit=20)["records"])
        assert any('"event": "ready"' in row["message"] for row in logs)
        check("bounded_journal_logs", count=len(logs))
        for mode, expected in (("success", "exited"), ("failure", "failed")):
            operation = command("start", mode)
            wait_for(lambda: unit(mode)["lifecycle"] == expected)
            assert unit(mode)["run_id"] == operation["run_id"]
            check(mode + "_without_restart")
        descendant = command("start", "descendant")
        wait_for(lambda: unit("descendant")["lifecycle"] == "descendants_surviving")
        assert unit("descendant")["members"]
        command("stop", "descendant", expected_run=descendant["run_id"])
        wait_for(lambda: not unit("descendant")["blocked"])
        check("setsid_descendant_survives_parent_and_stops")
        slow = command("start", "slow")
        wait_for(lambda: (root / (token + "-slow.jsonl")).exists())
        operation = command("stop", "slow", expected_run=slow["run_id"])
        wait_for(lambda: request(state, "operation", request_id=operation["request_id"])["phase"] == "timed_out")
        assert unit("slow")["members"]
        wait_for(lambda: not unit("slow")["blocked"])
        events = [json.loads(line) for line in (root / (token + "-slow.jsonl")).read_text().splitlines()]
        assert len([event for event in events if event["event"] == "signal"]) == 1
        assert any(event["event"] == "exit" for event in events)
        assert request(state, "operation", request_id=operation["request_id"])["phase"] == "timed_out"
        check("timeout_survives_then_self_exits_with_one_signal")
        def competing_start(name):
            try: return (name, command("start", name))
            except Fault as exc: return (name, exc.code)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(competing_start, ("idle", "peer")))
        accepted = [(name, value) for name, value in results if isinstance(value, dict)]
        rejected = [(name, value) for name, value in results if isinstance(value, str)]
        assert len(accepted) == len(rejected) == 1 and rejected[0][1] == "unit_conflict", results
        winner, started = accepted[0]
        loser = rejected[0][0]
        assert unit(loser)["start_blocked_by"] == [winner]
        manager.terminate()
        manager.wait(timeout=8)
        manager = launch()
        wait_for(lambda: request(state, "status")["fresh"])
        assert competing_start(loser) == (loser, "unit_conflict")
        command("stop", winner, expected_run=started["run_id"])
        wait_for(lambda: not unit(winner)["blocked"])
        replacement = command("start", loser)
        command("stop", loser, expected_run=replacement["run_id"])
        wait_for(lambda: not unit(loser)["blocked"])
        check("concurrent_conflicting_starts_allow_one_survive_manager_restart_and_release_after_stop", winner=winner)
        report["passed"] = True
    except BaseException as exc:
        report.update(passed=False, error=repr(exc))
        raise
    finally:
        errors = []
        if manager and manager.poll() is None:
            try:
                for value in status()["units"]:
                    if value["blocked"] and not value.get("stop_request_id"):
                        command("stop", value["unit_id"], expected_run=value["run_id"])
                wait_for(lambda: not any(value["blocked"] for value in status()["units"]), seconds=10)
                report["final_status"] = status()
            except Exception as exc:
                errors.append(repr(exc))
            manager.terminate()
            manager.wait(timeout=8)
        report["cleanup_errors"] = errors
        (root / (token + "-integration.json")).write_text(json.dumps(report, indent=2))
        (root / "latest-integration.txt").write_text(token)
        log.close()
        if errors:
            raise RuntimeError("inspect isolated cleanup report")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
