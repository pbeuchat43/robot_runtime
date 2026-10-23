"""Serialized ROS policy, observations and durable intent shared by adapters."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import hashlib
import queue
import uuid

from .common import Fault, dumps, fields
from .core import public_operation, request_id
from .ros_codec import arguments
from .parameter_runtime import ParameterRuntime
from .snapshot_runtime import SnapshotRuntime
from .ros_profile import MAX_TOPICS
from .action_runtime import ActionRuntime, COMMANDS as ACTION_COMMANDS
from .observation_delivery import split_topic


PENDING = ("prepared", "in_progress")
UNCERTAIN = ("timed_out", "outcome_unknown")
MUTATIONS = ("ros_service", "ros_parameter_set", "ros_topic_command")


class RosRuntime:
    def __init__(self, core):
        self.core = core
        self.config = core.profile.get("ros")
        self.topics = {spec["id"]: spec for spec in self.config["topics"]} if self.config else {}
        self.services = {spec["id"]: spec for spec in self.config["services"]} if self.config else {}
        self.commands = {spec["id"]: spec for spec in self.config.get("commands", [])} if self.config else {}
        self.transport = None
        self.generation = None
        self.ready = False
        self.error = "ROS is not configured" if not self.config else "ROS worker is starting"
        self.schemas, self.samples, self.listeners = {}, {}, {}
        self.update_listeners = {}
        self.service_availability = {}
        self.command_availability = {}
        self.capability_revision = 0
        self.bindings = {}
        self.subscriptions = []
        self.active = None
        self.next_restart = 0
        self.closed = False
        self.parameters = ParameterRuntime(self)
        self.snapshots = SnapshotRuntime(self)
        self.actions = ActionRuntime(self)
        for operation in core.operations.values():
            if operation["action"] in MUTATIONS and operation["phase"] in PENDING:
                core._save_operation(operation, phase="outcome_unknown", outcome="manager_restarted")
            elif operation["action"] == "ros_parameter_set" and operation.get("readback", {}).get("status") == "pending":
                core._save_operation(operation, readback=dict(status="unavailable", error="manager_restarted"))

    def summary(self):
        return dict(enabled=bool(self.config), generation=self.generation, available=self.ready,
                    error=self.error, subscriptions=self.subscriptions, capability_revision=self.capability_revision,
                    snapshot_capture=self.snapshots.summary())

    def live_run(self, unit_id):
        run = self.core.current(unit_id)
        status = self.core.status()
        unit = next((unit for unit in status["units"] if unit["unit_id"] == unit_id), None)
        if (not status["fresh"] or not run or not unit or unit["ownership"] != "owned"
                or run["lifecycle"] != "running" or run.get("stop_request_id")):
            return None
        return run

    def snapshot(self, selected=None, *, catalog=True):
        chosen = set(self.topics) if selected is None else set(selected)
        now = self.core.clock()
        status = self.core.status()
        live = {unit["unit_id"]: self.core.current(unit["unit_id"]) for unit in status["units"]
                if status["fresh"] and unit["ownership"] == "owned" and unit["lifecycle"] == "running"
                and not unit.get("stop_request_id")}
        topics = []
        for key, spec in self.topics.items():
            if key not in chosen:
                continue
            run = live.get(spec["unit_id"])
            sample = self.samples.get(key, {}) if self.ready else {}
            if not run or sample.get("run_id") != run["run_id"]:
                sample = {}
            received = sample.get("received_monotonic")
            age = max(0, now - received) if received is not None else None
            acquisition = sample.get("acquisition_age_at_receipt")
            acquisition_age = acquisition + age if acquisition is not None and age is not None else None
            sample_age = sample.get("sample_age_at_receipt")
            sample_age = sample_age + age if sample_age is not None and age is not None else None
            progress = sample.get("progress_monotonic")
            progress_age = max(0, now - progress) if progress is not None else None
            fresh = bool(self.ready and run and sample.get("status") == "current" and age is not None and age <= spec["stale_after_seconds"])
            if acquisition_age is not None and acquisition_age > spec["stale_after_seconds"]:
                fresh = False
            if sample_age is not None and sample_age >= spec["stale_after_seconds"]:
                fresh = False
            value = {name: spec[name] for name in ("id", "label", "unit_id", "node", "name", "type", "max_rate_hz", "stale_after_seconds")}
            value.update(sample, fields=self.schemas.get(key, {}).get("fields", {}), age_seconds=age, fresh=fresh,
                         status=sample.get("status", "awaiting_sample" if run and key in self.bindings else "not_observed"),
                         acquisition_timestamp=(sample.get("data") or {}).get("acquisition_timestamp_ns"),
                         acquisition_age_seconds=acquisition_age, sample_age_seconds=sample_age, progress_age_seconds=progress_age,
                         projection=spec.get("projection"), source_clock="rmw_source_timestamp", receipt_clock="host_monotonic")
            if not self.ready:
                value["status"] = "worker_unavailable"
            elif not run:
                value["status"] = "runtime_unavailable"
            elif not fresh and value["status"] == "current":
                old_stamp = ((acquisition_age is not None and acquisition_age > spec["stale_after_seconds"])
                             or (sample_age is not None and sample_age >= spec["stale_after_seconds"]))
                value["status"] = "stale_source" if old_stamp else "stale"
            topics.append(value)
        header = dict(api_version=1, config_revision=self.core.revision, boot_id=self.core.boot,
                      server_time_ms=int(self.core.wall() * 1000), **self.summary(), topics=topics)
        if not catalog:
            return header
        services = []
        for key, spec in self.services.items():
            run = self.live_run(spec["unit_id"])
            value = {name: spec[name] for name in ("id", "label", "unit_id", "node", "name", "type", "timeout_seconds")}
            value.update(self.schemas.get(key, {}), run_id=run["run_id"] if run else None,
                         blocked_by=self.unresolved(run["run_id"]) if run else None,
                         available=bool(self.ready and run and self.service_availability.get(key, {}).get("available")),
                         error=self.service_availability.get(key, {}).get("error", "awaiting_discovery"))
            services.append(value)
        commands = []
        for key, spec in self.commands.items():
            run = self.live_run(spec["unit_id"])
            commands.append(dict(spec, run_id=run["run_id"] if run else None,
                blocked_by=self.unresolved(run["run_id"]) if run else None,
                available=bool(self.ready and run and self.command_availability.get(key, {}).get("available")),
                error=self.command_availability.get(key, {}).get("error", "awaiting_discovery")))
        return dict(header, services=services,
                    parameters=self.parameters.catalog(), snapshots=self.snapshots.catalog(), actions=self.actions.catalog(), commands=commands)

    def publish(self, changed=None):
        for channel, chosen in tuple(self.listeners.items()):
            if channel in self.update_listeners:
                dirty = self.update_listeners[channel]["dirty"]
                dirty.update(chosen if changed is None else chosen.intersection(changed))
                if changed is not None and not dirty:
                    continue
                value = True
            else:
                value = self.snapshot(chosen)
            try:
                channel.get_nowait()
            except queue.Empty:
                pass
            channel.put_nowait(value)

    def add(self, topics, *, updates=False):
        if not isinstance(topics, list) or not topics or len(topics) > MAX_TOPICS or any(not isinstance(key, str) or key not in self.topics for key in topics):
            raise Fault("not_found", "Choose configured ROS topic IDs")
        if len(self.listeners) >= 8:
            raise Fault("busy", "ROS watcher limit reached")
        channel = queue.Queue(maxsize=1)
        self.listeners[channel] = set(topics)
        if updates:
            self.update_listeners[channel] = dict(dirty=set(topics), reset=True, metadata={})
        self.runtime_changed()
        if channel.empty():
            channel.put_nowait(True if updates else self.snapshot(topics))
        return channel

    def take_updates(self, channel):
        # Called on the manager thread after a wakeup. Overwritten notifications
        # cannot lose a topic: dirty IDs accumulate, data remains latest-only.
        pending = self.update_listeners[channel]
        value = self.snapshot(pending["dirty"], catalog=False)
        value["reset"] = pending["reset"]
        value["sampled_monotonic"] = self.core.clock()
        metadata, topics = [], []
        for topic in value["topics"]:
            spec, sample = split_topic(topic)
            if spec != pending["metadata"].get(topic["id"]):
                pending["metadata"][topic["id"]] = spec
                metadata.append(spec)
            topics.append(sample)
        value.update(metadata=metadata, topics=topics)
        pending["dirty"].clear()
        pending["reset"] = False
        return value

    def remove(self, channel):
        self.listeners.pop(channel, None)
        self.update_listeners.pop(channel, None)
        self.runtime_changed()

    def runtime_changed(self):
        self.snapshots.runtime_changed()
        self.actions.sync()
        desired = set().union(*self.listeners.values()) if self.listeners else set()
        bindings = {}
        for key in desired:
            run = self.live_run(self.topics[key]["unit_id"])
            if run:
                bindings[key] = run["run_id"]
        if bindings != self.bindings:
            self.samples = {key: value for key, value in self.samples.items() if bindings.get(key) == value.get("run_id")}
            self.bindings = bindings
            if self.ready:
                try:
                    self.transport.send(dict(kind="demand", bindings=bindings))
                except Fault:
                    self.lost("ROS demand could not be delivered")
            self.publish()

    def unresolved(self, run_id):
        return next((op["request_id"] for op in self.core.operations.values()
                     if op["action"] in MUTATIONS and op["run_id"] == run_id
                     and op["phase"] in UNCERTAIN and not op.get("uncertainty_acknowledged")), None)

    def changed(self):
        self.capability_revision += 1
        self.publish()
        self.core.changed()

    def lost(self, reason):
        self.ready, self.error = False, reason
        self.samples.clear()
        self.service_availability.clear()
        self.command_availability.clear()
        self.subscriptions = []
        self.parameters.lost(reason)
        self.actions.lost("worker_disconnected")
        if self.active:
            operation = self.core.operations[self.active]
            if operation["phase"] in PENDING:
                self.core._save_operation(operation, phase="outcome_unknown", outcome="worker_disconnected")
            elif operation.get("readback", {}).get("status") == "pending":
                self.core._save_operation(operation, readback=dict(status="unavailable", error="worker_disconnected"))
            self.active = None
        self.next_restart = self.core.clock() + 2
        self.changed()

    def start(self):
        self.generation = uuid.uuid4().hex
        self.ready = False
        self.schemas, self.samples = {}, {}
        self.service_availability = {}
        self.command_availability = {}
        self.parameters.lost("worker_restarted")
        self.actions.lost("worker_restarted")
        self.error = "ROS worker is starting"
        self.next_restart = self.core.clock() + 5
        try:
            self.transport.start(self.generation)
        except (Fault, OSError):
            self.error = "ROS worker could not start; inspect the manager journal"
        self.changed()

    def event(self, value):
        if self.closed or value.get("generation") != self.generation:
            return
        kind = value.get("kind")
        if kind == "lost":
            self.lost("ROS worker disconnected; inspect the manager journal")
        elif kind == "status":
            unchanged = (self.ready == bool(value["ready"]) and self.schemas == value["schemas"]
                         and self.subscriptions == value["subscriptions"]
                         and self.parameters.availability == value.get("parameter_availability", {})
                         and self.actions.availability == value.get("action_availability", {})
                         and self.command_availability == value.get("command_availability", {})
                         and self.service_availability == value.get("service_availability", {}))
            if unchanged:
                return
            was_ready = self.ready
            self.ready, self.error = bool(value["ready"]), None
            self.schemas = value["schemas"]
            self.subscriptions = value["subscriptions"]
            self.parameters.availability = value.get("parameter_availability", {})
            self.actions.availability = value.get("action_availability", {})
            self.command_availability = value.get("command_availability", {})
            availability = value.get("service_availability", {})
            if availability != self.service_availability:
                self.service_availability = availability
            if not was_ready:
                self.transport.send(dict(kind="demand", bindings=self.bindings))
            self.changed()
        elif kind in ("action_update", "action_feedback", "action_cancel_result"):
            self.actions.event(value)
        elif kind == "topic":
            key = value["topic_id"]
            if self.bindings.get(key) == value.get("run_id"):
                self.samples[key] = {k: v for k, v in value.items() if k not in ("kind", "generation", "topic_id")}
                self.publish({key})
        elif kind == "parameter_read":
            self.parameters.event(value)
        elif kind == "parameter_readback":
            operation = self.core.operations.get(value["request_id"])
            if not operation or operation.get("readback", {}).get("status") != "pending":
                return
            run = self.live_run(operation["unit_id"])
            readback = value["readback"]
            if not run or run["run_id"] != operation["run_id"]:
                readback = dict(status="unavailable", error="runtime_changed")
            elif self.core.clock() >= operation["deadline_monotonic"]:
                readback = dict(status="unavailable", error="response_deadline_exceeded")
            self.core._save_operation(operation, readback=readback)
            if self.active == operation["request_id"]:
                self.active = None
            self.changed()
        elif kind == "operation":
            operation = self.core.operations.get(value["request_id"])
            if not operation or operation["phase"] not in PENDING:
                return
            run = self.live_run(operation["unit_id"])
            known_parameter_result = (operation["action"] == "ros_parameter_set" and value.get("phase") == "complete"
                and value.get("delivery") == "responded" and self.core.clock() < operation["deadline_monotonic"])
            if known_parameter_result:
                result = {k: value[k] for k in ("phase", "delivery", "outcome", "response", "readback") if k in value}
                if not run or run["run_id"] != operation["run_id"]:
                    if result.get("readback", {}).get("status") == "pending":
                        result["readback"] = dict(status="unavailable", error="runtime_changed")
                    try:
                        self.transport.send(dict(kind="cancel", request_id=operation["request_id"]))
                    except Fault:
                        pass
                self.core._save_operation(operation, **result)
            elif not run or run["run_id"] != operation["run_id"]:
                self.core._save_operation(operation, phase="outcome_unknown", outcome="runtime_changed")
            elif self.core.clock() >= operation["deadline_monotonic"]:
                self.core._save_operation(operation, phase="timed_out", outcome="response_deadline_exceeded")
            else:
                self.core._save_operation(operation, **{k: value[k] for k in ("phase", "delivery", "outcome", "response", "readback") if k in value})
            if operation["phase"] not in PENDING and operation.get("readback", {}).get("status") != "pending":
                self.active = None
            self.changed()

    def tick(self):
        if self.closed or not self.config:
            return
        if self.transport and not self.transport.running() and self.core.clock() >= self.next_restart:
            self.start()
        self.parameters.tick()
        self.actions.tick()
        if self.active:
            operation = self.core.operations[self.active]
            if self.core.clock() >= operation["deadline_monotonic"]:
                if operation.get("readback", {}).get("status") == "pending":
                    self.core._save_operation(operation, readback=dict(status="unavailable", error="response_deadline_exceeded"))
                elif operation["phase"] in PENDING:
                    self.core._save_operation(operation, phase="timed_out", outcome="response_deadline_exceeded")
                try:
                    self.transport.send(dict(kind="cancel", request_id=self.active))
                except Fault:
                    pass
                self.active = None
                self.changed()
        # Invalidate run-bound demand even if the supervisor's last audit ages out.
        self.runtime_changed()

    def require_run(self, unit_id, expected):
        self.core._require_available()
        current = self.core.current(unit_id)
        if not current or current["run_id"] != expected:
            raise Fault("stale_run", "The requested runtime run is no longer current")
        if not self.live_run(unit_id):
            raise Fault("precondition", "ROS commands require an owned running unit with no stop request")
        return current

    def command(self, value, actor):
        parameter = value["action"] == "ros_parameter_set"
        topic_command = value["action"] == "ros_topic_command"
        payload = ("changes", "expected_snapshot") if parameter else ("choice",) if topic_command else ("arguments",)
        fields(value, ("version", "action", "capability_id", "request_id", "expected_run", "generation", "config_revision", "expires_at_ms", *payload))
        rid = request_id(value["request_id"])
        fingerprint = hashlib.sha256(dumps(value).encode()).hexdigest()
        previous = self.core.operations.get(rid)
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise Fault("request_conflict", "Request ID was used with different arguments")
            return public_operation(previous)
        self.core.store.capacity()
        key = value["capability_id"]
        registry = self.parameters.specs if parameter else self.commands if topic_command else self.services
        if not isinstance(key, str) or key not in registry:
            raise Fault("not_found", "Unknown ROS command capability")
        expiry = value["expires_at_ms"]
        remaining_ms = expiry - int(self.core.wall() * 1000) if type(expiry) is int else -1
        if not 0 < remaining_ms <= 30000:
            raise Fault("expired", "Command expiry must be in the next 30 seconds")
        dispatch_deadline = self.core.clock() + remaining_ms / 1000
        if value["config_revision"] != self.core.revision:
            raise Fault("stale_config", "Profile revision changed")
        if not self.ready:
            raise Fault("ros_unavailable", "ROS worker is unavailable")
        if value["generation"] != self.generation:
            raise Fault("stale_generation", "ROS worker generation changed")
        if self.active or self.parameters.active:
            raise Fault("busy", "Another ROS request is pending")
        spec = registry[key]
        run = self.require_run(spec["unit_id"], value["expected_run"])
        unresolved = self.unresolved(run["run_id"])
        if unresolved:
            raise Fault("outcome_unknown", "Inspect and locally acknowledge the previous uncertain ROS request", request_id=unresolved)
        baseline = self.parameters.prepare(value) if parameter else None
        if topic_command:
            choice = next((item for item in spec["choices"] if item["id"] == value["choice"]), None)
            if choice is None:
                raise Fault("invalid_arguments", "Choose a declared topic command")
            arguments(choice["arguments"], self.schemas[key]["request"])
        elif not parameter:
            arguments(value["arguments"], self.schemas[key]["request"])
        if self.core.clock() >= dispatch_deadline or self.core.wall() * 1000 >= expiry:
            raise Fault("expired", "Command expired during validation")
        operation = dict(request_id=rid, action=value["action"], capability_id=key, unit_id=spec["unit_id"], run_id=run["run_id"],
                         fingerprint=fingerprint, actor=actor, phase="prepared", delivery="unknown", generation=self.generation,
                         created_at_ms=int(self.core.wall() * 1000), expires_at_ms=expiry, dispatch_deadline=dispatch_deadline,
                         deadline_monotonic=self.core.clock() + spec["timeout_seconds"], boot_id=self.core.boot)
        if topic_command:
            operation["choice"] = value["choice"]
        self.core.store.save(operation=operation)
        self.core.operations[rid] = operation
        self.active = rid
        self.parameters.invalidate(spec["unit_id"])
        try:
            if parameter:
                self.transport.send(dict(kind="parameter_set", request_id=rid, capability_id=key, changes=value["changes"],
                    baseline=baseline, dispatch_deadline=dispatch_deadline, deadline=operation["deadline_monotonic"]))
            elif topic_command:
                self.transport.send(dict(kind="topic_command", request_id=rid, capability_id=key, choice=value["choice"], dispatch_deadline=dispatch_deadline))
            else:
                self.transport.send(dict(kind="call", request_id=rid, capability_id=key, arguments=value["arguments"], dispatch_deadline=dispatch_deadline))
        except Fault:
            self.core._save_operation(operation, phase="outcome_unknown", outcome="worker_delivery_unknown")
            self.active = None
        self.changed()
        return public_operation(operation)

    def handle(self, value, actor):
        action = value["action"]
        if action == "ros_actions":
            fields(value, ("version", "action"))
            return self.actions.snapshot()
        if action in ACTION_COMMANDS:
            return self.actions.command(value, actor)
        if action == "ros_action_authorize":
            return self.actions.authorize(value)
        if action == "ros_status":
            fields(value, ("version", "action"))
            return self.snapshot()
        if action == "ros_parameters":
            return self.parameters.read(value)
        if action in MUTATIONS:
            return self.command(value, actor)
        if action == "ros_authorize":
            fields(value, ("version", "action", "request_id", "generation"))
            rid = request_id(value["request_id"])
            operation = self.core.operations.get(rid)
            if not operation or rid != self.active or operation["phase"] != "prepared" or value["generation"] != self.generation:
                raise Fault("stale_generation", "ROS dispatch is no longer authorized")
            self.require_run(operation["unit_id"], operation["run_id"])
            if self.core.clock() >= min(operation["dispatch_deadline"], operation["deadline_monotonic"]) or self.core.wall() * 1000 >= operation["expires_at_ms"]:
                raise Fault("expired", "ROS command expired before dispatch")
            self.core._save_operation(operation, phase="in_progress")
            self.changed()
            return {k: operation[k] for k in ("dispatch_deadline", "deadline_monotonic")}
        if action == "ros_acknowledge":
            fields(value, ("version", "action", "request_id", "expected_run"))
            operation = self.core.operations.get(request_id(value["request_id"]))
            if not operation or operation["action"] not in MUTATIONS:
                raise Fault("not_found", "Unknown ROS operation")
            self.require_run(operation["unit_id"], value["expected_run"])
            if operation["run_id"] != value["expected_run"] or operation["phase"] not in UNCERTAIN:
                raise Fault("precondition", "Only an uncertain operation for this run can be acknowledged")
            self.core._save_operation(operation, uncertainty_acknowledged=True, acknowledged_at_ms=int(self.core.wall() * 1000), acknowledged_by=actor)
            self.changed()
            return public_operation(operation)
        raise Fault("invalid_request", "Unsupported ROS action")

    def close(self):
        self.closed = True
        self.lost("ROS manager is stopping")
        if self.transport:
            self.transport.close()
