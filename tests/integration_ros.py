"""Actual Jazzy + systemd + CLI verification in an isolated local-only domain."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import argparse
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from robot_runtime.common import Fault
from robot_runtime.ipc import decode_response, request
from integration_runtime import wait_for
from ros_fixture import make_profile


def main(root):
    root = root.resolve()
    assert root.parent == Path("/tmp") and re.fullmatch(r"robot-runtime-ros-[0-9a-f]{12}", root.name)
    token = uuid.uuid4().hex[:10]
    area = root / ("ros-"+token)
    area.mkdir(mode=0o700)
    state = area/"state"
    profile = make_profile(area, token)
    log = (area/"manager.log").open("w")
    report = dict(token=token, checks=[], domain_id=221, discovery="LOCALHOST",
                  source_sha256={str(p.relative_to(Path(__file__).resolve().parent.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted(Path(__file__).resolve().parent.parent.joinpath("src").rglob("*")) if p.is_file() and "__pycache__" not in p.parts})
    manager, duplicate = None, None
    watches = []

    def launch():
        return subprocess.Popen([sys.executable,"-m","robot_runtime.manager","--profile",str(area/"profile.json"),"--state",str(state)], stdout=log, stderr=log)
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
        temporary = area/"control.tmp"
        temporary.write_text(json.dumps(values))
        os.replace(temporary, area/"control.json")
    def check(name, **values):
        report["checks"].append(dict(check=name, passed=True, **values))
    def lifecycle(action, **values):
        return local(action, unit_id="idle", request_id=str(uuid.uuid4()), expires_at_ms=int(time.time()*1000)+10000,
                     config_revision=status()["config_revision"], **values)
    def service(**changes):
        value = dict(capability_id="enable", request_id=str(uuid.uuid4()), expires_at_ms=int(time.time()*1000)+10000,
                     config_revision=status()["config_revision"], expected_run=unit()["run_id"], generation=ros()["generation"], arguments=dict(data=True))
        value.update(changes)
        return value
    def operation(rid):
        return local("operation", request_id=rid)
    def complete(rid):
        return wait_for(lambda: operation(rid) if operation(rid)["phase"] not in ("prepared","in_progress") else None, seconds=8)
    def topic(key="counter"):
        return next(value for value in ros()["topics"] if value["id"]==key)
    def watch(topics):
        connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        connection.settimeout(10)
        connection.connect(str(state/"runtime.sock"))
        connection.sendall((json.dumps(dict(version=1,action="ros_watch",topics=topics))+"\n").encode())
        reader=connection.makefile("rb")
        watches.append((connection,reader))
        value=decode_response(reader.readline())
        assert value["type"]=="snapshot"
        return connection,reader
    def close_watches():
        for connection,reader in watches:
            reader.close()
            connection.close()
        watches.clear()

    try:
        manager=launch()
        wait_for(lambda: status()["fresh"] and ros()["available"], seconds=15)
        assert unit()["lifecycle"]=="stopped" and ros()["subscriptions"]==[]
        check("manager_loads_ros_interfaces_without_launch_or_subscription")
        started=lifecycle("start")
        wait_for(lambda: unit()["lifecycle"]=="running" and (area/"provider-status.json").exists(),seconds=15)
        time.sleep(0.7)
        assert provider()["counter_subscribers"]==0
        check("runtime_only_view_does_not_subscribe")
        streams=[watch(["counter"]) for _ in range(5)]
        sample=wait_for(lambda: topic() if topic()["fresh"] else None,seconds=15)
        assert isinstance(sample["data"]["data"],str) and int(sample["data"]["data"])>2**53
        assert sample["source_timestamp_ns"] is None or isinstance(sample["source_timestamp_ns"],str)
        wait_for(lambda: provider()["counter_subscribers"]==1)
        check("five_watchers_share_one_subscription_and_int64_is_lossless",sample=sample)
        observed=[]
        until=time.monotonic()+2
        reader=streams[0][1]
        while time.monotonic()<until:
            event=decode_response(reader.readline())
            if event.get("type")=="snapshot":
                value=event["status"]["topics"][0]
                if value.get("received_monotonic"):
                    observed.append(value["received_monotonic"])
        unique=sorted(set(observed))
        if len(unique)>1:
            assert (len(unique)-1)/(unique[-1]-unique[0])<=6
        check("sample_delivery_coalesces_20hz_source_to_5hz",distinct_samples=len(unique))
        control(publish=False)
        wait_for(lambda: topic()["status"]=="stale")
        age=topic()["age_seconds"]
        time.sleep(0.3)
        assert topic()["age_seconds"]>age and not topic()["fresh"]
        control()
        wait_for(lambda: topic()["fresh"])
        close_watches()
        wait_for(lambda: provider()["counter_subscribers"]==0 and not ros()["subscriptions"],seconds=12)
        check("staleness_is_not_refreshed_and_last_consumer_releases_subscription")
        watch(["text"])
        wait_for(lambda: topic("text")["fresh"])
        control(text="x"*4096)
        wait_for(lambda: topic("text")["status"]=="sample_too_large")
        control(text="x"*90)
        wait_for(lambda: topic("text")["status"]=="invalid_sample")
        control(text="")
        wait_for(lambda: topic("text")["fresh"] and topic("text")["data"]["data"]=="")
        check("raw_and_json_limits_invalidate_old_data_and_fresh_empty_replaces_it")
        close_watches()
        command=service()
        saved=local("ros_service",**command)
        result=complete(saved["request_id"])
        assert result["phase"]=="complete" and result["outcome"]=="provider_accepted", result
        calls=provider()["calls"]
        assert local("ros_service",**command)["request_id"]==saved["request_id"]
        time.sleep(0.2)
        assert provider()["calls"]==calls
        cli=subprocess.run([sys.executable,"-m","robot_runtime.cli","--state",str(state),"ros","call","enable",
                            "--expected-run",unit()["run_id"],"--arguments",json.dumps(dict(data=False))],capture_output=True,text=True,check=True,timeout=12)
        result=complete(json.loads(cli.stdout)["request_id"])
        assert result["outcome"]=="provider_rejected" and result["response"]["success"] is False
        check("service_success_refusal_cli_and_durable_deduplication")
        for changes,code in ((dict(expected_run="stale"),"stale_run"),(dict(generation="stale"),"stale_generation"),(dict(arguments=dict(data="true")),"invalid_arguments")):
            try:
                local("ros_service",**service(**changes))
                raise AssertionError("Invalid ROS command accepted")
            except Fault as exc:
                assert exc.code==code,(exc.code,code)
        check("stale_run_generation_and_wrong_typed_arguments_are_rejected")
        duplicate=subprocess.Popen(["/bin/bash","--noprofile","--norc",str(area/"launch-ros.sh"),str(Path(__file__).with_name("ros_fixture.py")),str(area),
                                    profile["ros"]["topics"][0]["node"].rsplit("/",1)[0],"--duplicate"],env={**profile["units"][0]["environment"]},stdout=log,stderr=log)
        watch(["counter"])
        wait_for(lambda: topic()["status"]=="source_ambiguous",seconds=10)
        before=provider()["calls"]
        result=complete(local("ros_service",**service())["request_id"])
        assert result["phase"]=="failed" and result["delivery"]=="not_sent",result
        assert provider()["calls"]==before
        duplicate.terminate()
        duplicate.wait(timeout=5)
        duplicate=None
        wait_for(lambda: topic()["fresh"],seconds=15)
        check("duplicate_graph_provider_blocks_observation_and_service_dispatch")
        close_watches()
        control(delay=1.6)
        before=provider()["calls"]
        timed=local("ros_service",**service())
        wait_for(lambda: provider()["calls"]==before+1)
        result=complete(timed["request_id"])
        assert result["phase"]=="timed_out",result
        try:
            local("ros_service",**service())
            raise AssertionError("Uncertain run was not blocked")
        except Fault as exc:
            assert exc.code=="outcome_unknown"
        time.sleep(1)
        assert provider()["calls"]==before+1 and operation(timed["request_id"])["phase"]=="timed_out"
        control()
        local("ros_acknowledge",request_id=timed["request_id"],expected_run=unit()["run_id"])
        check("timeout_does_not_replay_or_cancel_remote_execution_and_requires_acknowledgement")
        generation=ros()["generation"]
        # The Jetson kernel does not expose task/<tid>/children. Inspect parent
        # identity once for this crash test; product code never scans /proc.
        worker_pids=[]
        for path in Path("/proc").glob("[0-9]*/status"):
            try:
                parent=next(line.split()[1] for line in path.read_text().splitlines() if line.startswith("PPid:"))
                if int(parent)==manager.pid and b"ros_worker.py" in (path.parent/"cmdline").read_bytes():
                    worker_pids.append(int(path.parent.name))
            except (FileNotFoundError,ProcessLookupError,PermissionError):
                pass
        assert len(worker_pids)==1
        os.kill(worker_pids[0],signal.SIGKILL)
        wait_for(lambda: ros()["available"] and ros()["generation"]!=generation,seconds=15)
        assert unit()["run_id"]==started["run_id"]
        check("ros_worker_restart_changes_generation_and_preserves_runtime")
        wait_for(lambda: ros()["services"][0]["available"],seconds=15)
        control(delay=1.6)
        before=provider()["calls"]
        uncertain=local("ros_service",**service())
        wait_for(lambda: provider()["calls"]==before+1)
        pid=unit()["main_pid"]
        manager.kill()
        manager.wait(timeout=5)
        manager=launch()
        wait_for(lambda: ros()["available"] and status()["fresh"],seconds=15)
        assert unit()["main_pid"]==pid and unit()["run_id"]==started["run_id"]
        assert operation(uncertain["request_id"])["phase"]=="outcome_unknown"
        time.sleep(1)
        assert provider()["calls"]==before+1
        check("manager_crash_preserves_provider_and_never_replays_uncertain_service")
        control()
        report["passed"]=True
    except BaseException as exc:
        report.update(passed=False,error=repr(exc))
        raise
    finally:
        errors=[]
        close_watches()
        if duplicate and duplicate.poll() is None:
            duplicate.terminate()
            duplicate.wait(timeout=5)
        if manager and manager.poll() is None:
            try:
                current=unit()
                if current["blocked"] and not current.get("stop_request_id"):
                    lifecycle("stop",expected_run=current["run_id"])
                wait_for(lambda: not unit()["blocked"],seconds=10)
                report["final_status"]=status()
            except Exception as exc:
                errors.append(repr(exc))
            manager.terminate()
            manager.wait(timeout=10)
        log.close()
        if "Fatal Python error" in (area/"manager.log").read_text():
            errors.append("ROS worker reported a fatal interpreter-shutdown error")
        report["cleanup_errors"]=errors
        (area/"report.json").write_text(json.dumps(report,indent=2))
        (root/"latest-ros.txt").write_text(area.name)
        if errors:
            raise RuntimeError("Inspect isolated ROS cleanup report")
    print(json.dumps(dict(passed=report["passed"],checks=len(report["checks"]),path=str(area/"report.json")),indent=2))


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("root",type=Path)
    main(parser.parse_args().root)
