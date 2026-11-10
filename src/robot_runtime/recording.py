"""Recording admission, immutable worker launch and bounded observations."""
# Copyright (c) 2026 Paul N. Beuchat. MIT License.
import copy
import json
import os
from pathlib import Path
import threading
import time

from .common import Fault, fields, loads
from .prepared import read_file, run_inputs
from .profile import identifier


def validate(value, units):
    fields(value, ("unit_id", "source_unit_id", "overlays_by_mode"))
    recorder, source = value["unit_id"], value["source_unit_id"]
    if recorder == source or recorder not in units or source not in units:
        raise Fault("invalid_profile", "Recording requires distinct declared recorder and source units")
    if source not in units[recorder]["requires_running"] or "preparation" not in units[recorder]:
        raise Fault("invalid_profile", "Recording requires source admission and prepared settings")
    choices = value["overlays_by_mode"]
    modes = {mode["id"] for mode in units[source].get("launch_modes", [])}
    if not isinstance(choices, dict) or set(choices) != modes:
        raise Fault("invalid_profile", "Declare recording choices for every source mode")
    for mode, overlays in choices.items():
        identifier(mode)
        if not isinstance(overlays, list) or any(x not in ("cones", "coco", "combined") for x in overlays) or len(set(overlays)) != len(overlays):
            raise Fault("invalid_profile", "Invalid recording overlay choices")
    return copy.deepcopy(value)


def options(value):
    fields(value, ("outputs",), ("overlay", "frame_index"))
    outputs = value["outputs"]
    if not isinstance(outputs, list) or not 1 <= len(outputs) <= 3 or any(type(x) is not str or x not in ("rgb", "depth", "annotated") for x in outputs) or len(set(outputs)) != len(outputs):
        raise Fault("invalid_request", "Choose distinct recording outputs: rgb, depth and/or annotated")
    annotated = "annotated" in outputs
    if annotated != ("overlay" in value) or (annotated and value["overlay"] not in ("cones", "coco", "combined")):
        raise Fault("invalid_request", "Annotated recording requires exactly one declared overlay")
    index = value.get("frame_index", True)
    if type(index) is not bool:
        raise Fault("invalid_request", "frame_index must be a boolean")
    return dict(outputs=sorted(outputs), frame_index=index, **({"overlay": value["overlay"]} if annotated else {}))


def is_recorder(core, unit_id):
    return core.profile.get("recording", {}).get("unit_id") == unit_id


def check_request(core, request):
    recorder = is_recorder(core, request.get("unit_id"))
    if recorder and "prepared" in request:
        raise Fault("invalid_request", "Recorder settings must pass current source admission; prepared overrides are unavailable")
    if recorder and request.get("action") == "restart":
        raise Fault("invalid_request", "Stop recording explicitly, then start a new session")
    if "recording" in request and (not recorder or request.get("action") != "start"):
        raise Fault("invalid_request", "Recording options are accepted only when starting the recorder")
    return options(request.get("recording", {"outputs": ["rgb"]})) if recorder and request.get("action") == "start" else None


def binding(core, request):
    selected = check_request(core, request)
    if selected is None:
        return None
    # Profile/catalog validation also imports this module in the standalone
    # bootstrap, where native D-Bus bindings are intentionally unavailable.
    from .supervisor import owns
    profile = core.profile["recording"]
    source = core.current(profile["source_unit_id"])
    if not source or source["lifecycle"] != "running" or source.get("stop_request_id") or not owns(source, core.observations.get(source["run_id"])):
        raise Fault("precondition", "Recording requires the current owned running camera source")
    mode = (source.get("launch_mode") or {}).get("id")
    if mode not in profile["overlays_by_mode"] or "prepared" not in source:
        raise Fault("precondition", "Source profile is unavailable for recording")
    if "overlay" in selected and selected["overlay"] not in profile["overlays_by_mode"][mode]:
        raise Fault("precondition", "Selected overlay is not running in the current source mode")
    return dict(options=selected, source_unit_id=source["unit_id"], source_run_id=source["run_id"],
                source_mode=mode, source_prepared=copy.deepcopy(source["prepared"]))


def observation(core, run):
    if not run:
        return dict(state="not_started", fresh=False)
    value = copy.deepcopy(core.recording_observations.get(run["run_id"]))
    if not value:
        return dict(state="unknown" if run["blocked"] else "interrupted", fresh=False,
                    detail="No valid worker observation", run_id=run["run_id"])
    terminal = value.get("state") in ("completed", "failed") and value.get("finalization") in ("complete", "incomplete")
    age = (int(core.clock() * 1e9) - int(value["heartbeat_ns"])) / 1e9 if value["boot_id"] == core.boot else None
    value["fresh"] = terminal or (age is not None and 0 <= age <= 3 and run["blocked"])
    value["observation_age_seconds"] = age
    if not terminal and (not run["blocked"] or run["boot_id"] != core.boot):
        # Process exit can precede the next asynchronous file read. Give that
        # observation a bounded chance to arrive without claiming completion or
        # briefly reporting an ordinary Stop as a crash.
        pending = run["boot_id"] == core.boot and age is not None and 0 <= age <= 3
        value.update(state="awaiting_final_status" if pending else "interrupted", fresh=False)
    elif not terminal and not value["fresh"]:
        value.update(state="unknown", detail="Worker observation is stale")
    return value


class Observations:
    """Read files outside the serialized policy owner; publish bounded snapshots."""
    def __init__(self, core, dispatch):
        self.core, self.dispatch = core, dispatch
        self.lock, self.closed = threading.Lock(), threading.Event()
        self.targets = []
        self.thread = threading.Thread(target=self.run, name="recording-observations", daemon=True)
        self.thread.start()

    def update(self):
        run = self.core.current(self.core.profile["recording"]["unit_id"])
        with self.lock:
            self.targets = [(run["run_id"], run["recording_status"])] if run and run.get("recording_status") else []

    def publish(self, results):
        if self.closed.is_set():
            return False
        if self.core.recording_observations != results:
            self.core.recording_observations = results
            self.core.changed()
        return False

    def run(self):
        while not self.closed.is_set():
            with self.lock:
                targets = list(self.targets)
            result = {}
            for run_id, path in targets:
                try:
                    value = loads(read_file(path, maximum=262144))
                    if (value.get("schema_version") != 1 or value.get("run_id") != run_id or
                        not isinstance(value.get("boot_id"), str) or not isinstance(value.get("heartbeat_ns"), str) or
                        not value["heartbeat_ns"].isdigit() or len(value["heartbeat_ns"]) > 20):
                        continue
                    result[run_id] = value
                except (Fault, ValueError, TypeError, AttributeError):
                    pass
            self.dispatch(lambda value=result: self.publish(value))
            self.closed.wait(1)

    def close(self):
        self.closed.set()
        self.thread.join(timeout=2)


def worker(argv=None):
    """Exec the installed native worker with the verified, frozen input file."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True)
    args = parser.parse_args(argv)
    manifest, inputs = run_inputs()
    if set(inputs) != {"recording"}:
        raise Fault("invalid_prepared", "Recorder requires its admitted settings")
    path = next(item["path"] for item in manifest["inputs"] if item["name"] == "recording")
    os.execv(args.executable, [args.executable, "--ros-args", "-p", "config_file:=" + path,
        "-p", "run_id:=" + json.dumps(os.environ["ROBOT_RUNTIME_RUN"]),
        "-p", "status_file:=" + os.environ["ROBOT_RUNTIME_RECORDING_STATUS"]])


if __name__ == "__main__":
    worker()
