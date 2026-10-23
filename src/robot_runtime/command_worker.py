"""Fixed-choice, single-publication ROS commands; publication is not acceptance."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import time

from .common import Fault
from .ipc import request
from .ros_codec import arguments, selected_schema


class CommandWorker:
    def __init__(self, worker):
        from rosidl_runtime_py.utilities import get_message
        self.worker, self.node = worker, worker.node
        self.specs = {spec["id"]: spec for spec in worker.config.get("commands", [])}
        self.types, self.availability = {}, {}
        for key, spec in self.specs.items():
            kind = self.types[key] = get_message(spec["type"])
            schema = selected_schema(kind(), spec["fields"], complete=True)
            worker.schemas[key] = dict(request=schema)
            for choice in spec["choices"]:
                arguments(choice["arguments"], schema)

    def target(self, key):
        spec = self.specs[key]
        endpoints = self.node.get_subscriptions_info_by_topic(spec["name"])
        if not endpoints:
            raise Fault("source_unavailable", "The declared command subscriber is unavailable")
        if (len(endpoints) != 1 or not self.worker.unique_node(spec["node"]) or endpoints[0].topic_type != spec["type"]
                or endpoints[0].node_namespace.rstrip("/") + "/" + endpoints[0].node_name != spec["node"]):
            raise Fault("source_ambiguous", "Commands require exactly one declared subscriber")

    def audit(self):
        values = {}
        for key in self.specs:
            try:
                self.target(key)
                values[key] = dict(available=True, error=None)
            except Fault as exc:
                values[key] = dict(available=False, error=exc.code)
        if values != self.availability:
            self.availability = values
            self.worker.status()

    def send(self, message):
        from rclpy.duration import Duration
        from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
        key, rid = message["capability_id"], message["request_id"]
        publisher, attempted = None, False
        try:
            spec = self.specs[key]
            choice = next(item for item in spec["choices"] if item["id"] == message["choice"])
            body = self.types[key]()
            for name, value in arguments(choice["arguments"], self.worker.schemas[key]["request"]).items():
                setattr(body, name, value)
            self.target(key)
            publisher = self.node.create_publisher(self.types[key], spec["name"],
                QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE))
            until = min(message["dispatch_deadline"], time.monotonic() + .5)
            while publisher.get_subscription_count() != 1 and time.monotonic() < until:
                self.worker.rclpy.spin_once(self.node, timeout_sec=.02)
            if publisher.get_subscription_count() != 1:
                raise Fault("source_unavailable", "Command publisher did not match the declared subscriber")
            authority = request(self.worker.state, "ros_authorize", request_id=rid, generation=self.worker.generation)
            self.target(key)
            if time.monotonic() >= min(authority["dispatch_deadline"], authority["deadline_monotonic"]):
                raise Fault("expired", "Command expired before publication")
            attempted = True
            publisher.publish(body)
            # Keep the finite publisher alive for middleware delivery. DDS ACKs
            # do not establish that the application accepted or acted on input.
            publisher.wait_for_all_acked(Duration(seconds=.2))
            self.worker.operation(rid, phase="complete", delivery="published", outcome="published_once",
                response=dict(application_acceptance="unconfirmed", note="Observe provider state separately; publication is not an acknowledgement."))
        except Exception as exc:
            self.worker.operation(rid, phase="outcome_unknown" if attempted else "failed", delivery="unknown" if attempted else "not_sent",
                outcome=exc.code if isinstance(exc, Fault) else "ros_error")
        finally:
            if publisher:
                self.node.destroy_publisher(publisher)
