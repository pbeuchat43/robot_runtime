"""Shared, on-demand captures with run binding and a separate bounded helper."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import os
import queue
import select
import subprocess
import threading
import time
import uuid

from .common import Fault, dumps, fields
from .ipc import MAX_RESPONSE, decode_response
from .ros_process import worker_launch


class SnapshotProcess:
    def __init__(self, config, state, schedule):
        self.config, self.state, self.schedule = config, state, schedule

    def start(self, job, callback):
        # Keep cleanup alive during orderly manager shutdown until this helper exits.
        threading.Thread(target=self.run, args=(job, callback), daemon=False).start()

    def run(self, job, callback):
        process = None
        result = Fault("snapshot_unavailable", "Snapshot helper could not complete")
        try:
            argv, environment = worker_launch(self.config, self.state, "snapshot_worker.py")
            environment.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
            process = subprocess.Popen(argv, cwd=self.state, env=environment, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=None)
            payload = (dumps(dict(spec=job["spec"], domain_id=self.config["domain_id"], deadline=job["deadline"] - 0.25,
                                  manager_pid=os.getpid())) + "\n").encode()
            process.stdin.write(payload)
            process.stdin.close()
            raw = bytearray()
            while not job["cancel"].is_set() and time.monotonic() < job["deadline"]:
                if not select.select([process.stdout], [], [], 0.1)[0]:
                    continue
                part = os.read(process.stdout.fileno(), min(16384, MAX_RESPONSE + 1 - len(raw)))
                raw.extend(part)
                if len(raw) > MAX_RESPONSE:
                    raise Fault("image_too_large", "Snapshot helper exceeded its output budget")
                if b"\n" in raw or not part:
                    result = decode_response(bytes(raw))
                    break
            else:
                result = Fault("snapshot_unavailable", "Snapshot cancelled or capture deadline exceeded")
        except Fault as exc:
            result = exc
        except (OSError, ValueError):
            pass
        finally:
            if process:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    # Only this private helper is killed, never the provider workload.
                    process.kill()
                    process.wait(timeout=1)
                process.stdout.close()
                if not process.stdin.closed:
                    process.stdin.close()
            self.schedule(lambda: (callback(job, result), False)[1])


class SnapshotRuntime:
    """Methods run on the manager thread; only the helper touches native images."""
    def __init__(self, ros):
        self.ros, self.core = ros, ros.core
        self.specs = {spec["id"]: spec for spec in (ros.config or {}).get("snapshots", [])}
        self.runner = None
        self.active = None
        self.next_capture = 0

    def binding(self, spec):
        run = self.ros.live_run(spec["unit_id"])
        if not run:
            raise Fault("snapshot_unavailable", "Start the workload before requesting a snapshot")
        if spec.get("required_mode") and (run.get("launch_mode") or {}).get("id") != spec["required_mode"]:
            raise Fault("snapshot_unavailable", "Images are disabled for this run. Stop it, then choose Start with images.")
        return dict(run_id=run["run_id"], generation=self.core.generation,
                    boot_id=self.core.boot, config_revision=self.core.revision)

    def catalog(self):
        result = []
        for spec in self.specs.values():
            item = dict(spec, available=False, error=None)
            try:
                item.update(self.binding(spec), available=bool(self.runner))
            except Fault as exc:
                item["error"] = exc.message
            result.append(item)
        return result

    def summary(self):
        job = self.active
        return (dict(snapshot_id=job["snapshot_id"], capability_id=job["spec"]["id"],
                     run_id=job["binding"]["run_id"], viewers=len(job["listeners"]), cancelling=job["cancel"].is_set())
                if job else None)

    def add(self, request):
        fields(request, ("version", "action", "capability_id", "expected_run", "generation", "config_revision"))
        if type(request["version"]) is not int or request["version"] != 1:
            raise Fault("protocol_version", "Expected IPC version 1")
        key = request["capability_id"]
        if not isinstance(key, str) or key not in self.specs:
            raise Fault("not_found", "Unknown snapshot capability")
        spec = self.specs[key]
        binding = self.binding(spec)
        if (binding["run_id"] != request["expected_run"] or binding["generation"] != request["generation"]
                or binding["config_revision"] != request["config_revision"]):
            raise Fault("stale_run", "Snapshot target changed; refresh its status")
        if self.runner is None:
            raise Fault("snapshot_unavailable", "Snapshot helper is unavailable")
        job = self.active
        if job:
            if job["spec"]["id"] != key or job["binding"] != binding or job["cancel"].is_set() or len(job["listeners"]) >= 5:
                raise Fault("busy", "A snapshot is already in progress")
        else:
            if self.core.clock() < self.next_capture:
                raise Fault("rate_limited", "Wait one second between captures")
            job = dict(spec=spec, binding=binding, snapshot_id=uuid.uuid4().hex, listeners=set(),
                       cancel=threading.Event(), deadline=self.core.clock() + spec["timeout_seconds"])
            self.active = job
        channel = queue.Queue(maxsize=1)
        job["listeners"].add(channel)
        if len(job["listeners"]) == 1:
            try:
                self.runner.start(job, self.finish)
            except Exception:
                self.active = None
                raise Fault("snapshot_unavailable", "Snapshot helper could not start")
        return job, channel

    def remove(self, job, channel):
        job["listeners"].discard(channel)
        if not job["listeners"]:
            job["cancel"].set()

    def validate_result(self, job, result):
        if isinstance(result, Fault):
            raise result
        if self.binding(job["spec"]) != job["binding"]:
            raise Fault("stale_run", "Snapshot belongs to a previous runtime observation")
        receipt_age = max(0, self.core.clock() - result["received_monotonic"])
        age = result["acquisition_age_at_receipt"] + receipt_age
        if age > job["spec"]["stale_after_seconds"]:
            raise Fault("stale_snapshot", "Snapshot acquisition is already stale")
        return dict(result, **job["binding"], snapshot_id=job["snapshot_id"], capability_id=job["spec"]["id"],
                    acquisition_age_seconds=age, receipt_age_seconds=receipt_age,
                    stale_after_seconds=job["spec"]["stale_after_seconds"])

    def finish(self, job, result):
        if self.active is job:
            self.active = None
            self.next_capture = self.core.clock() + 1
        for channel in job["listeners"]:
            if channel.empty():
                channel.put_nowait(result)

    def runtime_changed(self):
        job = self.active
        if job:
            try:
                if self.binding(job["spec"]) != job["binding"]:
                    raise Fault("stale_run", "Snapshot runtime changed")
            except Fault as exc:
                job["cancel"].set()
                for channel in job["listeners"]:
                    if channel.empty():
                        channel.put_nowait(exc)

    def close(self):
        if self.active:
            self.active["cancel"].set()
