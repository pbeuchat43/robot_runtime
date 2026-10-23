"""On-demand parameter observations, shared by local and browser clients."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import hashlib
import uuid

from .common import Fault, dumps, fields
from .parameter_codec import changes


class ParameterRuntime:
    def __init__(self, ros):
        self.ros, self.core = ros, ros.core
        self.specs = {spec["id"]: spec for spec in ros.config.get("parameters", [])} if ros.config else {}
        self.samples, self.results, self.next_read = {}, {}, {}
        self.active = None
        self.availability = {}

    def spec(self, key):
        if not isinstance(key, str) or key not in self.specs:
            raise Fault("not_found", "Unknown parameter capability")
        return self.specs[key]

    def catalog(self):
        values = []
        for key, spec in self.specs.items():
            run = self.ros.live_run(spec["unit_id"])
            available = self.availability.get(key, {})
            values.append(dict(spec, run_id=run["run_id"] if run else None,
                available=bool(self.ros.ready and run and available.get("available")),
                writable=bool(self.ros.ready and run and available.get("writable")),
                error=available.get("error", "awaiting_discovery"),
                snapshot_id=self.samples.get(key, {}).get("snapshot_id"),
                blocked_by=self.ros.unresolved(run["run_id"]) if run else None))
        return values

    def snapshot(self, key):
        spec = self.spec(key)
        run = self.ros.live_run(spec["unit_id"])
        sample = self.samples.get(key)
        if not sample or not run or sample["run_id"] != run["run_id"] or sample["generation"] != self.ros.generation:
            sample = {}
        age = max(0, self.core.clock() - sample["received_monotonic"]) if sample else None
        available = self.availability.get(key, {}).get("available", False)
        fresh = bool(sample and self.ros.ready and available and age <= spec["stale_after_seconds"])
        reading = bool(self.active and self.active["key"] == key)
        result = self.results.get(key, {})
        status = "current" if fresh else "stale" if sample else "not_read"
        if not self.ros.ready:
            status = "worker_unavailable"
        elif not run:
            status = "runtime_unavailable"
        elif not available:
            status = "source_unavailable"
        return dict(api_version=1, capability_id=key, config_revision=self.core.revision, generation=self.ros.generation,
            boot_id=self.core.boot, run_id=run["run_id"] if run else None, status=status, fresh=fresh,
            age_seconds=age, stale_after_seconds=spec["stale_after_seconds"],
            snapshot_id=sample.get("snapshot_id"), parameters=sample.get("parameters", {}),
            reading=reading, read_id=self.active["id"] if reading else result.get("read_id"),
            read_status="pending" if reading else result.get("status", "not_requested"),
            error=result.get("error"), received_monotonic=sample.get("received_monotonic"))

    def read(self, value):
        fields(value, ("version", "action", "capability_id", "refresh"))
        if type(value["refresh"]) is not bool:
            raise Fault("invalid_request", "refresh must be boolean")
        key = value["capability_id"]
        spec = self.spec(key)
        if not value["refresh"]:
            return self.snapshot(key)
        self.core._require_available()
        run = self.ros.live_run(spec["unit_id"])
        if not run:
            raise Fault("precondition", "Parameter inspection requires an owned running unit; it does not start it")
        if not self.ros.ready:
            raise Fault("ros_unavailable", "ROS worker is unavailable")
        if self.active and self.active["key"] == key and self.active["run_id"] == run["run_id"]:
            return self.snapshot(key)
        if self.active or self.ros.active:
            raise Fault("busy", "Another ROS request is pending")
        if self.core.clock() < self.next_read.get(key, 0):
            raise Fault("rate_limited", "Wait one second between parameter refreshes")
        rid = uuid.uuid4().hex
        self.active = dict(id=rid, key=key, run_id=run["run_id"], deadline=self.core.clock() + spec["timeout_seconds"])
        self.next_read[key] = self.core.clock() + 1
        try:
            self.ros.transport.send(dict(kind="parameter_read", request_id=rid, capability_id=key,
                deadline=self.active["deadline"]))
        except Fault:
            self.active = None
            raise
        self.ros.changed()
        return self.snapshot(key)

    def event(self, value):
        active = self.active
        if not active or value["request_id"] != active["id"]:
            return
        self.active = None
        key = active["key"]
        run = self.ros.live_run(self.specs[key]["unit_id"])
        error = value.get("error")
        if not run or run["run_id"] != active["run_id"]:
            error = "runtime_changed"
        elif self.core.clock() >= active["deadline"]:
            error = "response_deadline_exceeded"
        if error:
            self.samples.pop(key, None)
            self.results[key] = dict(read_id=active["id"], status="failed", error=error)
        else:
            sample = dict(parameters=value["parameters"], received_monotonic=value["received_monotonic"],
                          run_id=active["run_id"], generation=self.ros.generation)
            identity = dict(parameters=sample["parameters"], run_id=active["run_id"],
                            generation=self.ros.generation, config_revision=self.core.revision)
            sample["snapshot_id"] = hashlib.sha256(dumps(identity).encode()).hexdigest()
            self.samples[key] = sample
            self.results[key] = dict(read_id=active["id"], status="complete", error=None)
        self.ros.changed()

    def prepare(self, value):
        key = value["capability_id"]
        spec, snapshot = self.spec(key), self.snapshot(key)
        if not snapshot["fresh"] or value["expected_snapshot"] != snapshot["snapshot_id"]:
            raise Fault("stale_snapshot", "Refresh parameters before applying edits")
        permissions = {item["name"]: item["write"] for item in spec["parameters"]}
        changes(value["changes"], snapshot["parameters"], permissions)
        return {name: snapshot["parameters"][name] for name in value["changes"]}

    def invalidate(self, unit_id):
        for key, spec in self.specs.items():
            if spec["unit_id"] == unit_id:
                self.samples.pop(key, None)

    def lost(self, reason):
        self.samples.clear()
        self.availability.clear()
        if self.active:
            self.results[self.active["key"]] = dict(read_id=self.active["id"], status="failed", error=reason)
            self.active = None

    def tick(self):
        if self.active and self.core.clock() >= self.active["deadline"]:
            rid = self.active["id"]
            self.event(dict(request_id=rid, error="response_deadline_exceeded"))
            try:
                self.ros.transport.send(dict(kind="cancel", request_id=rid))
            except Fault:
                pass
