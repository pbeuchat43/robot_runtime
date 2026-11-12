"""Exact, durable Docker ownership; CLI I/O runs outside the policy thread."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import copy
import json
import re
import subprocess
import threading

from .common import Fault, fields, text


def validate(spec):
    from .profile import absolute, identifier, launch_argv, ENVIRONMENT
    fields(spec, ("image", "argv", "environment", "mounts", "gpu", "prepared_input"))
    identifier(spec["prepared_input"])
    if not isinstance(spec["image"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", spec["image"]):
        raise Fault("invalid_profile", "Docker requires an installed immutable image ID")
    launch_argv(spec["argv"])
    if type(spec["gpu"]) is not bool or not isinstance(spec["mounts"], list) or len(spec["mounts"]) > 8:
        raise Fault("invalid_profile", "Invalid Docker GPU or mount declaration")
    targets = {"/run/robot/prepared.json"}
    for mount in spec["mounts"]:
        fields(mount, ("source", "target"))
        for path in mount.values():
            absolute(path)
            if any(c in path for c in (",", "\n")):
                raise Fault("invalid_profile", "Unsupported Docker mount path")
        if mount["target"] in targets or "docker.sock" in mount["source"]:
            raise Fault("invalid_profile", "Duplicate/reserved Docker mount")
        targets.add(mount["target"])
    env = spec["environment"]
    if not isinstance(env, dict) or len(env) > 32:
        raise Fault("invalid_profile", "Docker environment exceeds its bound")
    for key, value in env.items():
        if not isinstance(key, str) or not ENVIRONMENT.fullmatch(key) or key.startswith("ROBOT_RUNTIME_"):
            raise Fault("invalid_profile", "Invalid Docker environment key")
        text(value, 4096, empty=True)


def intent(run):
    from .prepared import load
    manifest, _ = load(run["prepared"])
    selected = next((item for item in manifest["inputs"] if item["name"] == run["spec"]["docker"]["prepared_input"]), None)
    if not selected or "," in selected["path"] or "\n" in selected["path"]:
        raise Fault("invalid_prepared", "Missing or unsupported Docker prepared input")
    return dict(name=run["name"].removesuffix(".service"), id=None, phase="prepared",
                config_path=selected["path"],
                labels={"robot.runtime.run": run["run_id"], "robot.runtime.service": run["name"]},
                stop_requested=False, signal_sent=False, remove_sent=False)


class Engine:
    """Bounded commands against the local daemon; no pull, shell or forced kill."""
    def call(self, *args):
        result = subprocess.run(["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", *args],
                                capture_output=True, text=True, timeout=8)
        if result.returncode or len(result.stdout) > 262144:
            raise RuntimeError("Docker operation failed")
        return result.stdout.strip()

    def inspect(self, run):
        resource = run["docker"]
        # Listing establishes daemon reachability; inspect errors never mean absence.
        ids = self.call("container", "ls", "-aq", "--no-trunc", "--filter", "name=^/" + resource["name"] + "$").splitlines()
        if not ids:
            return dict(state="absent")
        if len(ids) != 1:
            return dict(state="conflict")
        value = json.loads(self.call("container", "inspect", ids[0]))[0]
        config, host = value["Config"], value["HostConfig"]
        if (value["Name"] != "/" + resource["name"] or value["Id"] != ids[0]
                or resource["id"] not in (None, value["Id"])
                or any(config.get("Labels", {}).get(k) != v for k, v in resource["labels"].items())
                or value["Image"] != run["spec"]["docker"]["image"]
                or host["RestartPolicy"]["Name"] != "no" or host["AutoRemove"]):
            return dict(state="conflict")
        state = value["State"]
        live = state["Running"] or state["Paused"] or state["Restarting"]
        return dict(state="live" if live else "stopped", id=value["Id"], exit_code=state["ExitCode"])

    def create(self, run):
        spec, resource = run["spec"]["docker"], run["docker"]
        args = ["container", "create", "--pull=never", "--name", resource["name"], "--restart=no",
                "--network=host", "--ipc=host", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--stop-signal=" + run["spec"]["stop"]["signal"]]
        if spec["gpu"]:
            args += ["--runtime=nvidia"]
        for key, value in resource["labels"].items():
            args += ["--label", key + "=" + value]
        for key, value in dict(spec["environment"], ROBOT_RUNTIME_RUN=run["run_id"]).items():
            args += ["--env", key + "=" + value]
        mounts = spec["mounts"] + [dict(source=resource["config_path"], target="/run/robot/prepared.json")]
        for mount in mounts:
            args += ["--mount", "type=bind,readonly,src=" + mount["source"] + ",dst=" + mount["target"]]
        args += [spec["image"], *spec["argv"]]
        result = self.call(*args)
        if not re.fullmatch(r"[0-9a-f]{64}", result):
            raise RuntimeError("Docker returned an invalid ID")
        observed = self.inspect(run)
        if observed.get("id") != result or observed["state"] != "stopped":
            raise RuntimeError("Docker create ownership was not established")
        return observed

    def start(self, run):
        observed = self.inspect(run)
        if observed.get("id") != run["docker"]["id"] or observed["state"] != "stopped":
            raise RuntimeError("Docker start ownership was not established")
        self.call("container", "start", run["docker"]["id"])
        return self.inspect(run)

    def signal(self, run):
        observed = self.inspect(run)
        if observed["state"] == "live":
            self.call("container", "kill", "--signal=" + run["spec"]["stop"]["signal"], observed["id"])
        elif observed["state"] not in ("stopped", "absent"):
            raise RuntimeError("Docker signal ownership was not established")
        return self.inspect(run)

    def remove(self, run):
        observed = self.inspect(run)
        if observed["state"] == "stopped":
            self.call("container", "rm", observed["id"])
        elif observed["state"] != "absent":
            raise RuntimeError("Docker removal requires a stopped owned container")
        return self.inspect(run)


class OwnedDocker:
    def __init__(self, core, deliver, engine=None):
        self.core, self.deliver, self.engine = core, deliver, engine or Engine()
        self.pending, self.observed = set(), {}
        self.closed = False
        for run in core.runs.values():
            if run["blocked"] and "docker" in run and run["docker"]["phase"] in ("creating", "starting"):
                self.save(run, phase="unknown")  # Recovery observes; never replay a mutation.

    def save(self, run, **values):
        self.core._save_run(run, docker=dict(run["docker"], **values))

    def submit(self, run, operation):
        rid = run["run_id"]
        if self.closed or rid in self.pending:
            return
        self.pending.add(rid)
        snapshot = copy.deepcopy(run)
        def work():
            try:
                result = getattr(self.engine, operation)(snapshot)
            except Exception:
                result = dict(state="unknown")
            self.deliver(lambda: self.completed(rid, operation, result))
        threading.Thread(target=work, daemon=True, name="owned-docker").start()

    def start(self, run):
        if "docker" not in run:
            return
        self.save(run, phase="creating")  # Durable before a daemon mutation.
        self.submit(run, "create")

    def stop(self, run):
        if "docker" in run:
            self.save(run, stop_requested=True)
            self.poll(run)

    def completed(self, rid, operation, result):
        if self.closed:
            return False
        self.pending.discard(rid)
        run = self.core.runs[rid]
        result["observed_monotonic"] = self.core.clock()
        self.observed[rid] = result
        if result.get("id"):
            self.save(run, id=result["id"])
        if operation == "create" and result["state"] == "stopped" and not run["docker"]["stop_requested"]:
            self.save(run, phase="starting")
            self.submit(run, "start")
        elif operation in ("create", "start"):
            self.save(run, phase="running" if result["state"] == "live" else "unknown")
        self.core.changed()
        return False

    def poll(self, run):
        if "docker" not in run or run["run_id"] in self.pending:
            return
        resource = run["docker"]
        observed = self.observed.get(run["run_id"])
        if resource["stop_requested"] and observed and observed.get("id") == resource["id"]:
            if observed["state"] == "live" and not resource["signal_sent"]:
                self.save(run, signal_sent=True)
                self.submit(run, "signal")
                return
            if observed["state"] == "stopped" and not resource["remove_sent"]:
                self.save(run, remove_sent=True)
                self.submit(run, "remove")
                return
        if not observed or self.core.clock() - observed["observed_monotonic"] >= .5:
            self.submit(run, "inspect")

    def state(self, run):
        if "docker" not in run:
            return "empty"
        self.poll(run)
        value = self.observed.get(run["run_id"])
        if not value or self.core.clock() - value["observed_monotonic"] > 3:
            return "unknown"
        if value["state"] == "absent":
            resource = run["docker"]
            # A missing create reply may still commit on this boot. Do not release
            # ownership on a negative lookup until found or the host has rebooted.
            if run["run_id"] in self.pending or (not resource["id"] and resource["phase"] != "prepared"
                                                and run["boot_id"] == self.core.boot):
                return "unknown"
            return "empty"
        return value["state"]

    def close(self):
        self.closed = True
