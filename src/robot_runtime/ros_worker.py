"""ROS-native process with private pipes; never imported by the manager/web."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import os
import queue
import select
import signal
import sys
import threading
import time
import uuid
from pathlib import Path

# Executed by the configured ROS interpreter from the installed package path.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robot_runtime.common import Fault, dumps, loads
from robot_runtime.ipc import MAX_RESPONSE, request
from robot_runtime.ros_codec import arguments, projection, selected_schema
from robot_runtime.ros_observations import observation_schema, project_observation
from robot_runtime.parameter_worker import ParameterWorker
from robot_runtime.action_worker import ActionWorker
from robot_runtime.command_worker import CommandWorker


class Output:
    """At most one replaceable value per topic, operation and worker status."""
    def __init__(self, stopped):
        self.condition = threading.Condition()
        self.latest = {}
        self.stopped = stopped
        threading.Thread(target=self.write, daemon=True).start()

    def send(self, key, value):
        raw = (dumps(value) + "\n").encode()
        if len(raw) > MAX_RESPONSE:
            raise Fault("response_limit", "ROS worker response exceeds its byte budget")
        with self.condition:
            if key not in self.latest and len(self.latest) >= 96:
                raise Fault("busy", "ROS output queue is full")
            self.latest[key] = raw
            self.condition.notify()

    def write(self):
        try:
            while not self.stopped.is_set():
                with self.condition:
                    self.condition.wait_for(lambda: self.latest or self.stopped.is_set(), timeout=0.5)
                    pending, self.latest = self.latest, {}
                for raw in pending.values():
                    view = memoryview(raw)
                    while view:
                        count = os.write(sys.stdout.fileno(), view)
                        view = view[count:]
        except (OSError, BrokenPipeError):
            self.stopped.set()


def full_name(name, namespace):
    return namespace.rstrip("/") + "/" + name


class Worker:
    def __init__(self, config, state, generation, output):
        import rclpy
        from rclpy.node import Node
        from rclpy.signals import SignalHandlerOptions
        from rosidl_runtime_py.utilities import get_message, get_service
        self.rclpy = rclpy
        self.config, self.state, self.generation, self.output = config, state, generation, output
        self.topics = {spec["id"]: spec for spec in config["topics"]}
        self.services = {spec["id"]: spec for spec in config["services"]}
        self.types, self.schemas = {}, {}
        self.subscriptions, self.samples, self.pending_raw = {}, {}, {}
        self.graph, self.next_emit = {}, {}
        self.service_availability = {}
        self.call = None
        self.node = None
        # Validate every exposed field before creating subscriptions or clients.
        for key, spec in self.topics.items():
            kind = get_message(spec["type"])
            self.types[key] = kind
            self.schemas[key] = (observation_schema(kind(), spec, get_message) if "projection" in spec
                                 else dict(fields=selected_schema(kind(), spec["fields"])))
        for key, spec in self.services.items():
            kind = get_service(spec["type"])
            self.types[key] = kind
            schema = dict(request=selected_schema(kind.Request(), spec["request_fields"], complete=True),
                          response=selected_schema(kind.Response(), spec["response_fields"]))
            if spec.get("success_field") and schema["response"][spec["success_field"]] not in ("bool", "boolean"):
                raise Fault("invalid_ros_profile", "Success field must be boolean")
            self.schemas[key] = schema
        rclpy.init(args=[], domain_id=config["domain_id"], signal_handler_options=SignalHandlerOptions.NO)
        self.node = Node("_robot_runtime_" + generation[:12], enable_rosout=False, start_parameter_services=False,
                         enable_logger_service=False)
        self.parameters = ParameterWorker(self)
        self.actions = ActionWorker(self)
        self.commands = CommandWorker(self)
        self.status()

    def status(self):
        self.output.send("status", dict(kind="status", generation=self.generation, ready=True,
                                        schemas=self.schemas, subscriptions=sorted(self.subscriptions),
                                        service_availability=self.service_availability,
                                        parameter_availability=self.parameters.availability,
                                        action_availability=self.actions.availability, command_availability=self.commands.availability))

    def emit(self, key):
        self.output.send("topic:" + key, dict(kind="topic", generation=self.generation, topic_id=key, **self.samples[key]))

    def invalidate(self, key, status):
        sample = self.samples[key]
        if sample.get("status") != status or sample.get("data") is not None:
            sample.update(status=status, data=None, received_monotonic=None, source_timestamp_ns=None,
                          acquisition_age_at_receipt=None, sample_age_at_receipt=None, progress_monotonic=None, progress_counter=None)
            self.pending_raw.pop(key, None)
            self.emit(key)

    def callback(self, key):
        def receive(raw, info):
            if key not in self.subscriptions:
                return
            sample, spec = self.samples[key], self.topics[key]
            sample["sequence"] += 1
            sample["received_count"] += 1
            if len(raw) > spec["max_serialized_bytes"]:
                sample["oversize_count"] += 1
                self.invalidate(key, "sample_too_large")
                return
            if not self.graph.get(key):
                return
            # Jazzy exposes source/receive times. Some RMW/rclpy versions also
            # expose a publisher GID; check it when present, without claiming
            # that either graph names or middleware IDs authenticate a process.
            gid = info.get("publisher_gid")
            if gid is not None and bytes(gid) != self.graph[key]:
                self.invalidate(key, "source_changed")
                return
            source = info.get("source_timestamp")
            self.pending_raw[key] = (raw, time.monotonic(), str(source) if source else None)
        return receive

    def demand(self, bindings):
        from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
        for key, (subscription, run_id) in list(self.subscriptions.items()):
            if bindings.get(key) != run_id:
                self.node.destroy_subscription(subscription)
                del self.subscriptions[key]
                self.samples.pop(key, None)
                self.pending_raw.pop(key, None)
                self.graph.pop(key, None)
                self.next_emit.pop(key, None)
        for key, run_id in bindings.items():
            if key in self.subscriptions:
                continue
            spec = self.topics[key]
            qos = QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=1, durability=DurabilityPolicy.VOLATILE,
                             reliability=ReliabilityPolicy.RELIABLE if spec["qos"]["reliability"] == "reliable" else ReliabilityPolicy.BEST_EFFORT)
            self.samples[key] = dict(run_id=run_id, stream_generation=uuid.uuid4().hex, sequence=0,
                                     received_count=0, oversize_count=0, status="awaiting_source", data=None,
                                     received_monotonic=None, source_timestamp_ns=None)
            subscription = self.node.create_subscription(self.types[key], spec["name"], self.callback(key), qos, raw=True)
            self.subscriptions[key] = (subscription, run_id)
            self.next_emit[key] = 0
            self.emit(key)
        self.status()

    def unique_node(self, name):
        return sum(full_name(node, namespace) == name for node, namespace in self.node.get_node_names_and_namespaces()) == 1

    def audit_topics(self):
        for key in self.subscriptions:
            spec = self.topics[key]
            endpoints = self.node.get_publishers_info_by_topic(spec["name"])
            valid = (len(endpoints) == 1 and self.unique_node(spec["node"])
                     and endpoints[0].topic_type == spec["type"]
                     and full_name(endpoints[0].node_name, endpoints[0].node_namespace) == spec["node"])
            previous = self.graph.get(key)
            self.graph[key] = bytes(endpoints[0].endpoint_gid) if valid else None
            if not valid:
                self.invalidate(key, "source_unavailable" if not endpoints else "source_ambiguous")
            elif previous != self.graph[key]:
                self.invalidate(key, "awaiting_sample")

    def service_target(self, spec):
        namespace, name = spec["node"].rsplit("/", 1)
        if self.node.count_services(spec["name"]) != 1 or not self.unique_node(spec["node"]):
            raise Fault("source_unavailable", "Service requires exactly one declared graph provider")
        targets = dict(self.node.get_service_names_and_types_by_node(name, namespace or "/"))
        if targets.get(spec["name"]) != [spec["type"]]:
            raise Fault("source_ambiguous", "Service name/type does not match the declared node")

    def audit_services(self):
        availability = {}
        for key, spec in self.services.items():
            try:
                self.service_target(spec)
                availability[key] = dict(available=True, error=None)
            except Fault as exc:
                availability[key] = dict(available=False, error=exc.code)
        if availability != self.service_availability:
            self.service_availability = availability
            self.status()

    def operation(self, rid, **values):
        self.output.send("operation:" + rid, dict(kind="operation", generation=self.generation, request_id=rid, **values))

    def start_call(self, message):
        rid, key = message["request_id"], message["capability_id"]
        client = None
        sent = False
        try:
            if self.call or self.parameters.task:
                raise Fault("busy", "A ROS service call is already pending")
            spec = self.services[key]
            values = arguments(message["arguments"], self.schemas[key]["request"])
            body = self.types[key].Request()
            for field, value in values.items():
                setattr(body, field, value)
            self.service_target(spec)
            client = self.node.create_client(self.types[key], spec["name"])
            # Resolve client discovery within a bounded admission window before
            # asking the manager for its final fresh run/expiry check.
            ready_deadline = min(message["dispatch_deadline"], time.monotonic() + 0.5)
            while not client.service_is_ready() and time.monotonic() < ready_deadline:
                self.rclpy.spin_once(self.node, timeout_sec=0.02)
            if not client.service_is_ready():
                raise Fault("source_unavailable", "Service client did not become ready")
            authority = request(self.state, "ros_authorize", request_id=rid, generation=self.generation)
            self.service_target(spec)
            if time.monotonic() >= authority["dispatch_deadline"]:
                raise Fault("expired", "Service command expired before dispatch")
            future = client.call_async(body)
            sent = True
            self.call = dict(id=rid, key=key, client=client, future=future, deadline=authority["deadline_monotonic"])
            self.operation(rid, phase="in_progress", delivery="sent")
        except Exception as exc:
            if client and not sent:
                self.node.destroy_client(client)
            code = exc.code if isinstance(exc, Fault) else "ros_error"
            self.operation(rid, phase="outcome_unknown" if sent else "failed", delivery="unknown" if sent else "not_sent", outcome=code)

    def cancel(self, rid):
        self.parameters.cancel(rid)
        if self.call and self.call["id"] == rid:
            call, self.call = self.call, None
            call["client"].remove_pending_request(call["future"])
            call["future"].cancel()
            self.node.destroy_client(call["client"])

    def progress(self):
        self.parameters.progress()
        self.actions.progress()
        from rclpy.serialization import deserialize_message
        now = time.monotonic()
        for key in self.subscriptions:
            spec, sample = self.topics[key], self.samples[key]
            if key in self.pending_raw and now >= self.next_emit[key]:
                raw, received, source = self.pending_raw.pop(key)
                self.next_emit[key] = now + 1 / spec["max_rate_hz"]
                try:
                    message = deserialize_message(raw, self.types[key])
                    value = (project_observation(message, spec) if "projection" in spec
                             else projection(message, self.schemas[key]["fields"], spec["max_json_bytes"]))
                    for stamp_kind in ("acquisition", "sample"):
                        if stamp_kind + "_timestamp_ns" not in value:
                            continue
                        # Account for time spent in the depth-one conversion slot.
                        age = (time.time_ns() - int(value[stamp_kind + "_timestamp_ns"])) / 1e9 - (now - received)
                        if age < -0.1:
                            raise Fault("timestamp_in_future", "Sample timestamp is ahead of the declared host clock")
                        sample[stamp_kind + "_age_at_receipt"] = max(0, age)
                    if "progress_counter" in value:
                        previous = sample.get("progress_counter")
                        previous = int(previous) if previous is not None else None
                        counter = int(value["progress_counter"])
                        if previous is None or counter < previous:
                            sample["progress_monotonic"] = None
                        elif counter > previous:
                            sample["progress_monotonic"] = received
                        sample["progress_counter"] = value["progress_counter"]
                    sample.update(status="current", data=value, received_monotonic=received, source_timestamp_ns=source)
                    self.emit(key)
                except Fault as exc:
                    self.invalidate(key, exc.code if "projection" in spec else "invalid_sample")
                except (ValueError, RuntimeError, TypeError, AttributeError):
                    self.invalidate(key, "invalid_sample")
            if sample["status"] == "current" and now - sample["received_monotonic"] > spec["stale_after_seconds"]:
                sample["status"] = "stale"
                self.emit(key)
        call = self.call
        if call:
            if now >= call["deadline"]:
                self.operation(call["id"], phase="timed_out", delivery="sent", outcome="response_deadline_exceeded")
                self.cancel(call["id"])
            elif call["future"].done():
                try:
                    response = projection(call["future"].result(), self.schemas[call["key"]]["response"])
                    success = self.services[call["key"]].get("success_field")
                    outcome = ("provider_accepted" if response[success] else "provider_rejected") if success else "response_received"
                    self.operation(call["id"], phase="complete", delivery="responded", outcome=outcome, response=response)
                except Exception:
                    self.operation(call["id"], phase="outcome_unknown", delivery="sent", outcome="invalid_response")
                self.cancel(call["id"])

    def close(self):
        self.actions.close()
        if self.parameters.task:
            self.parameters.cancel(self.parameters.task["id"])
        if self.call:
            self.cancel(self.call["id"])
        if self.node:
            self.node.destroy_node()
        if self.rclpy.ok():
            self.rclpy.shutdown()


def main():
    stopped = threading.Event()
    output = Output(stopped)
    commands = queue.Queue(maxsize=8)
    def read():
        pending = bytearray()
        try:
            while not stopped.is_set():
                if not select.select([sys.stdin.fileno()], [], [], 0.2)[0]:
                    continue
                raw = os.read(sys.stdin.fileno(), 65536)
                if not raw:
                    break
                pending.extend(raw)
                while b"\n" in pending:
                    line, _, remaining = pending.partition(b"\n")
                    if len(line) + 1 > MAX_RESPONSE:
                        raise Fault("request_limit", "ROS worker request is too large")
                    pending = bytearray(remaining)
                    commands.put(loads(line), timeout=1)
                if len(pending) > MAX_RESPONSE:
                    raise Fault("request_limit", "ROS worker request is too large")
        except (OSError, Fault, queue.Full):
            pass
        finally:
            stopped.set()
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stopped.set())
    worker = None
    try:
        initial = commands.get(timeout=15)
        if initial.get("kind") != "init":
            raise Fault("invalid_request", "Expected ROS worker configuration")
        worker = Worker(initial["config"], initial["state"], initial["generation"], output)
        next_graph = 0
        while not stopped.is_set():
            while not commands.empty():
                message = commands.get_nowait()
                if message["kind"] == "demand":
                    worker.demand(message["bindings"])
                    next_graph = 0
                elif message["kind"] == "call":
                    worker.start_call(message)
                elif message["kind"] in ("parameter_read", "parameter_set"):
                    worker.parameters.start(message)
                elif message["kind"] == "cancel":
                    worker.cancel(message["request_id"])
                elif message["kind"] == "action_goal":
                    worker.actions.start_goal(message)
                elif message["kind"] == "action_cancel":
                    worker.actions.cancel(message)
                elif message["kind"] == "action_track":
                    worker.actions.track(message["goals"])
                elif message["kind"] == "topic_command":
                    worker.commands.send(message)
            worker.rclpy.spin_once(worker.node, timeout_sec=0.05)
            if time.monotonic() >= next_graph:
                worker.audit_topics()
                worker.audit_services()
                worker.parameters.audit()
                worker.actions.audit()
                worker.commands.audit()
                next_graph = time.monotonic() + 0.5
            worker.progress()
    except Exception as exc:
        print("ROS worker failed:", type(exc).__name__, str(exc)[:500], file=sys.stderr, flush=True)
        raise
    finally:
        if worker:
            worker.close()
        stopped.set()
        reader.join(timeout=1.5)
        with output.condition:
            output.condition.notify_all()


if __name__ == "__main__":
    main()
