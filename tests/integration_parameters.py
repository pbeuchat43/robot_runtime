"""Native Jazzy parameter transactions, faults and restart recovery on jah."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

from robot_runtime.common import Fault
from robot_runtime.ipc import request
from integration_runtime import wait_for
from parameter_fixture import make_profile


def main(root):
    root = root.resolve()
    assert root.parent == Path("/tmp") and re.fullmatch(r"robot-runtime-ros-[0-9a-f]{12}", root.name)
    token = uuid.uuid4().hex[:10]
    area = root / ("parameters-" + token); area.mkdir(mode=0o700)
    state = area / "state"
    make_profile(area, token)
    log = (area / "manager.log").open("w")
    report = dict(checks=[], domain_id=222, discovery="LOCALHOST", source_sha256={
        str(p.relative_to(Path(__file__).resolve().parent.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path(__file__).resolve().parent.parent.joinpath("src").rglob("*") if p.is_file() and "__pycache__" not in p.parts})
    manager = None
    def launch():
        return subprocess.Popen([sys.executable, "-m", "robot_runtime.manager", "--profile", str(area/"profile.json"), "--state", str(state)], stdout=log, stderr=log)
    def local(action, **values):
        return request(state, action, **values)
    def status():
        return local("status", fresh=True)
    def unit():
        return status()["units"][0]
    def ros():
        return local("ros_status")
    def provider():
        return json.loads((area/"provider-status.json").read_text())
    def control(**values):
        path = area / "control.tmp"; path.write_text(json.dumps(values)); os.replace(path, area/"control.json")
    def check(name, **values):
        report["checks"].append(dict(check=name, passed=True, **values))
    def lifecycle(action, **values):
        return local(action, unit_id="idle", request_id=str(uuid.uuid4()), expires_at_ms=int(time.time()*1000)+10000,
            config_revision=status()["config_revision"], **values)
    def read():
        # The one-second manager throttle is part of the public contract.
        time.sleep(1.05)
        local("ros_parameters", capability_id="settings", refresh=True)
        value = wait_for(lambda: (v if not v["reading"] else None) if (v:=local("ros_parameters",capability_id="settings",refresh=False)) else None, seconds=5)
        assert value["fresh"] and value["read_status"] == "complete", value
        return value
    def command(snapshot, changes=None, **values):
        result = dict(capability_id="settings", request_id=str(uuid.uuid4()), expires_at_ms=int(time.time()*1000)+10000,
            config_revision=snapshot["config_revision"], expected_run=snapshot["run_id"], generation=snapshot["generation"],
            expected_snapshot=snapshot["snapshot_id"], changes=changes or dict(title=dict(type="string",value="edited")))
        result.update(values); return result
    def operation(rid):
        return local("operation", request_id=rid)
    def complete(rid):
        return wait_for(lambda: (v if v["phase"] not in ("prepared","in_progress") and v.get("readback",{}).get("status")!="pending" else None)
            if (v:=operation(rid)) else None, seconds=8)
    def write(snapshot, changes=None, **values):
        return complete(local("ros_parameter_set", **command(snapshot,changes,**values))["request_id"])
    def fault(code, function):
        try: function()
        except Fault as exc: assert exc.code == code, (exc.code,code)
        else: raise AssertionError("Expected " + code)
    try:
        manager = launch()
        wait_for(lambda: status()["fresh"] and ros()["available"], seconds=15)
        fault("precondition", lambda: local("ros_parameters", capability_id="settings", refresh=True))
        started = lifecycle("start")
        wait_for(lambda: ros()["parameters"][0]["available"] and (area/"provider-status.json").exists(), seconds=15)
        before = provider()["counts"]
        time.sleep(1.2)
        assert provider()["counts"] == before
        check("inspection_never_starts_workload_and_has_no_background_reads")
        control(read_delay=0.3)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: local("ros_parameters", capability_id="settings", refresh=True), range(5)))
        assert len({v["read_id"] for v in results}) == 1
        wait_for(lambda: not local("ros_parameters", capability_id="settings", refresh=False)["reading"])
        assert provider()["counts"]["get"] == before["get"]+1
        control()
        sample = read()
        values = sample["parameters"]
        assert values["count"]["value"] == "9007199254740993" and values["counts"]["value"] == [str(-(2**63)),str(2**63-1)]
        assert values["bytes"]["value"] == [0,255] and not values["identity"]["writable"] and not values["private"]["writable"]
        assert values["rate"]["descriptor"]["floating_point_range"][0]["to_value"] == 20.0
        check("five_readers_share_one_native_read_and_all_types_descriptors_are_lossless", sample=sample)
        all_types = {"enabled":dict(type="bool",value=False),"count":dict(type="integer",value=str(2**63-1)),
            "rate":dict(type="double",value=8.0),"title":dict(type="string",value="native"),"bytes":dict(type="byte_array",value=[1,254]),
            "flags":dict(type="bool_array",value=[False,True]),"counts":dict(type="integer_array",value=[str(-(2**63)),"9007199254740993"]),
            "rates":dict(type="double_array",value=[1.25,2.5]),"titles":dict(type="string_array",value=["", "changed"])}
        cmd = command(sample, all_types)
        result = complete(local("ros_parameter_set", **cmd)["request_id"])
        assert result["outcome"] == "provider_accepted" and result["readback"]["matches_requested"], result
        sets = provider()["counts"]["set"]
        assert local("ros_parameter_set", **cmd)["request_id"] == result["request_id"]
        time.sleep(0.2); assert provider()["counts"]["set"] == sets
        fault("stale_snapshot", lambda: local("ros_parameter_set", **command(sample)))
        check("all_nine_native_types_atomic_write_readback_dedup_and_other_client_invalidation", operation=result)
        sample = read()
        for changes, code in ((dict(identity=dict(type="string",value="x")),"read_only"), (dict(private=dict(type="integer",value="1")),"read_only"),
                             (dict(rate=dict(type="integer",value="1")),"invalid_arguments")):
            fault(code, lambda: local("ros_parameter_set", **command(sample,changes)))
        for override, code in ((dict(generation="old"),"stale_generation"),(dict(expected_run="old"),"stale_run")):
            fault(code, lambda: local("ros_parameter_set", **command(sample,**override)))
        rejected = write(sample,dict(title=dict(type="string",value="must-not-change"),rate=dict(type="double",value=13.0)))
        assert rejected["outcome"] == "provider_rejected" and provider()["title"] == "native", rejected
        rejected_range = write(read(),dict(rate=dict(type="double",value=100.0)))
        assert rejected_range["outcome"] == "provider_rejected"
        check("permissions_types_stale_targets_and_native_atomic_callback_range_rejection")
        sample = read(); control(external_title="external before dispatch")
        wait_for(lambda: provider()["title"] == "external before dispatch")
        sets = provider()["counts"]["set"]
        conflict = write(sample)
        assert conflict["outcome"] == "parameter_conflict" and conflict["delivery"] == "not_sent" and provider()["counts"]["set"] == sets, conflict
        control(); sample = read(); control(mismatch=True)
        mismatch = write(sample)
        assert mismatch["outcome"] == "provider_accepted" and not mismatch["readback"]["matches_requested"], mismatch
        check("external_change_conflict_and_post_acceptance_readback_mismatch", mismatch=mismatch)
        control(); sample = read(); control(readback_delay=4)
        lost = write(sample)
        assert lost["phase"] == "complete" and lost["outcome"] == "provider_accepted" and lost["readback"]["status"] == "unavailable", lost
        time.sleep(1.2); control()
        check("accepted_write_keeps_acceptance_when_readback_times_out", operation=lost)
        sample = read(); control(set_delay=4)
        timed = write(sample)
        assert timed["phase"] == "timed_out", timed
        fault("outcome_unknown", lambda: local("ros_parameter_set", **command(sample)))
        time.sleep(1.2); control()
        local("ros_acknowledge", request_id=timed["request_id"], expected_run=unit()["run_id"])
        check("mutation_timeout_is_uncertain_requires_acknowledgement_and_is_not_replayed")
        cli = subprocess.run([sys.executable,"-m","robot_runtime.cli","--state",str(state),"ros","parameters","read","settings"], capture_output=True,text=True,check=True,timeout=15)
        sample = json.loads(cli.stdout)
        cli = subprocess.run([sys.executable,"-m","robot_runtime.cli","--state",str(state),"ros","parameters","set","settings",
            "--expected-run",sample["run_id"],"--expected-snapshot",sample["snapshot_id"],"--changes",json.dumps(dict(title=dict(type="string",value="cli")))],capture_output=True,text=True,check=True,timeout=15)
        assert complete(json.loads(cli.stdout)["request_id"])["outcome"] == "provider_accepted"
        check("cli_reads_and_writes_native_parameters")
        sample = read(); control(set_delay=4)
        sets = provider()["counts"]["set"]
        uncertain = local("ros_parameter_set", **command(sample))
        wait_for(lambda: provider()["counts"]["set"] == sets+1)
        pid = unit()["main_pid"]
        manager.kill(); manager.wait(timeout=5); manager = launch()
        wait_for(lambda: ros()["available"] and status()["fresh"], seconds=15)
        assert unit()["run_id"] == started["run_id"] and unit()["main_pid"] == pid
        assert operation(uncertain["request_id"])["phase"] == "outcome_unknown"
        time.sleep(4); control(); assert provider()["counts"]["set"] == sets+1
        local("ros_acknowledge", request_id=uncertain["request_id"], expected_run=unit()["run_id"])
        check("manager_restart_preserves_run_records_uncertainty_and_never_replays")
        sample = read(); control(readback_delay=4)
        accepted = local("ros_parameter_set", **command(sample))
        wait_for(lambda: operation(accepted["request_id"]).get("readback",{}).get("status") == "pending")
        generation = ros()["generation"]
        worker_pids = []
        for path in Path("/proc").glob("[0-9]*/status"):
            try:
                parent = next(line.split()[1] for line in path.read_text().splitlines() if line.startswith("PPid:"))
                if int(parent) == manager.pid and b"ros_worker.py" in (path.parent/"cmdline").read_bytes(): worker_pids.append(int(path.parent.name))
            except (FileNotFoundError, ProcessLookupError, PermissionError): pass
        assert len(worker_pids) == 1
        os.kill(worker_pids[0], signal.SIGKILL)
        wait_for(lambda: ros()["available"] and ros()["generation"] != generation, seconds=15)
        recovered = operation(accepted["request_id"])
        assert recovered["outcome"] == "provider_accepted" and recovered["readback"]["status"] == "unavailable", recovered
        assert not local("ros_parameters",capability_id="settings",refresh=False)["fresh"]
        check("worker_loss_preserves_received_acceptance_and_invalidates_parameter_cache")
        control(); time.sleep(2)
        report["passed"] = True
    except BaseException as exc:
        report.update(passed=False,error=repr(exc)); raise
    finally:
        errors = []
        if manager and manager.poll() is None:
            try:
                if unit()["blocked"] and not unit().get("stop_request_id"): lifecycle("stop",expected_run=unit()["run_id"])
                wait_for(lambda: not unit()["blocked"], seconds=10)
                report["final_status"] = status()
            except Exception as exc: errors.append(repr(exc))
            manager.terminate(); manager.wait(timeout=10)
        log.close()
        report["cleanup_errors"] = errors
        (area/"report.json").write_text(json.dumps(report,indent=2)); (root/"latest-parameters.txt").write_text(area.name)
        if errors: raise RuntimeError("Inspect parameter cleanup report")
    print(json.dumps(dict(passed=report["passed"], checks=len(report["checks"]), path=str(area/"report.json"))))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("root", type=Path)
    main(parser.parse_args().root)
