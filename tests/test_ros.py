"""ROS policy tests without importing native ROS or opening a DDS participant."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import copy
import io
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from robot_runtime.common import Fault, dumps
from robot_runtime.core import Core
from robot_runtime.ipc import MAX_RESPONSE, decode_response
from robot_runtime.manager import Handler
from robot_runtime.profile import validate
from robot_runtime.ros_codec import arguments, projection, scalar, selected_schema
from robot_runtime.ros_runtime import RosRuntime
from robot_runtime.ros_web import TopicFanout
from robot_runtime.store import Store
from test_core import Clock, Supervisor, profile


def ros_config():
    return dict(schema_version=1, python="/usr/bin/python3", setup_scripts=["/opt/ros/jazzy/setup.bash"], domain_id=221,
                discovery="LOCALHOST", topics=[dict(id="counter", label="Counter", unit_id="worker", node="/fixture", name="/fixture/counter",
                    type="std_msgs/msg/Int64", fields=["data"], qos=dict(reliability="best_effort", durability="volatile", depth=1),
                    max_rate_hz=5, stale_after_seconds=1, max_serialized_bytes=1024, max_json_bytes=1024)],
                services=[dict(id="enable", label="Enable", unit_id="worker", node="/fixture", name="/fixture/enable",
                    type="std_srvs/srv/SetBool", request_fields=["data"], response_fields=["success", "message"], success_field="success", timeout_seconds=2)])


class Transport:
    def __init__(self):
        self.messages = []
    def send(self, value):
        self.messages.append(copy.deepcopy(value))
    def running(self):
        return True
    def close(self):
        pass


class RosTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name)
        os.chmod(self.path, 0o700)
        config = dict(profile(), ros=ros_config())
        self.config, revision = validate(config, check_paths=False)
        self.clock = Clock()
        self.supervisor = Supervisor(self.clock)
        self.store = Store(self.path)
        self.core = Core(self.config, revision, self.store, "boot", clock=self.clock, wall=self.clock.wall)
        self.core.connected(self.supervisor)
        operation = self.core.handle(dict(version=1, action="start", unit_id="worker", request_id=str(uuid.uuid4()),
                                        expires_at_ms=int(self.clock.wall()*1000)+10000, config_revision=revision))
        self.run = self.core.runs[operation["run_id"]]
        self.ros = self.core.ros = RosRuntime(self.core)
        self.ros.transport = Transport()
        self.ros.generation, self.ros.ready = "ros-generation", True
        self.ros.schemas = dict(counter=dict(fields=dict(data="int64")), enable=dict(request=dict(data="boolean"), response=dict(success="boolean", message="string")))

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def command(self, **values):
        return dict(version=1, action="ros_service", capability_id="enable", request_id=str(uuid.uuid4()),
                    expected_run=self.run["run_id"], generation=self.ros.generation, config_revision=self.core.revision,
                    expires_at_ms=int(self.clock.wall()*1000)+10000, arguments=dict(data=True), **values)

    def assertFault(self, code, callback):
        with self.assertRaises(Fault) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def authorize(self, operation):
        return self.core.handle(dict(version=1, action="ros_authorize", request_id=operation["request_id"], generation=self.ros.generation))

    def test_profile_rejects_unbounded_or_unmapped_capabilities(self):
        for field, value in (("name", "/bad//name"), ("unit_id", "missing"), ("fields", [["nested"]]), ("max_rate_hz", 1000), ("max_json_bytes", True)):
            bad = copy.deepcopy(self.config)
            bad["ros"]["topics"][0][field] = value
            with self.assertRaises(Fault):
                validate(bad, check_paths=False)
        bad = copy.deepcopy(self.config)
        bad["ros"]["discovery"] = "SUBNET"
        self.assertFault("invalid_ros_profile", lambda: validate(bad, check_paths=False))

    def test_48_topic_catalog_and_demand_are_bounded_together(self):
        source = copy.deepcopy(self.config)
        template = source["ros"]["topics"][0]
        source["ros"]["topics"] = [dict(template, id="topic"+str(i), name="/fixture/topic"+str(i)) for i in range(48)]
        config, _revision = validate(source, check_paths=False)
        self.core.profile = config
        ros = RosRuntime(self.core)
        ros.transport, ros.generation, ros.ready = Transport(), "test", True
        selected = [spec["id"] for spec in source["ros"]["topics"]]
        channel = ros.add(selected)
        self.assertEqual(set(ros.bindings), set(selected))
        self.assertEqual(len(channel.get_nowait()["topics"]), 48)
        self.assertFault("not_found", lambda: ros.add(selected+["topic0"]))
        source["ros"]["topics"].append(dict(template, id="topic48", name="/fixture/topic48"))
        self.assertFault("invalid_ros_profile", lambda: validate(source, check_paths=False))
        ros.remove(channel)
        self.assertFalse(ros.bindings)

    def test_256_kib_topic_budget_fits_snapshot_transport(self):
        source = copy.deepcopy(self.config)
        template = source["ros"]["topics"][0]
        source["ros"]["topics"] = [dict(template, id="topic"+str(i), name="/fixture/topic"+str(i),
            type="std_msgs/msg/String", max_serialized_bytes=16384, max_json_bytes=8192) for i in range(32)]
        config, _revision = validate(source, check_paths=False)
        source["ros"]["topics"][-1]["max_json_bytes"] += 1
        self.assertFault("invalid_ros_profile", lambda: validate(source, check_paths=False))
        self.core.profile = config
        ros = RosRuntime(self.core)
        ros.transport, ros.generation, ros.ready = Transport(), "test", True
        selected = list(ros.topics)
        channel = ros.add(selected)
        # Escaped Unicode exercises encoded JSON bytes, while respecting the string limit.
        data = dict(data=scalar("\u00e9" * 1363 + "xxx", "string"))
        self.assertEqual(len(dumps(data).encode()), 8192)
        for key in selected:
            ros.event(dict(kind="topic", generation=ros.generation, topic_id=key, run_id=self.run["run_id"],
                status="current", data=data, received_monotonic=self.clock(), stream_generation=key))
        snapshot = channel.get_nowait()
        self.assertTrue(all(topic["fresh"] for topic in snapshot["topics"]))
        self.assertEqual(sum(len(dumps(topic["data"]).encode()) for topic in snapshot["topics"]), 262144)
        handler = object.__new__(Handler)
        handler.wfile = io.BytesIO()
        handler.send(dict(ok=True, result=dict(type="snapshot", status=snapshot)))
        raw = handler.wfile.getvalue()
        self.assertGreater(len(raw), 262144)
        self.assertEqual(decode_response(raw)["status"], snapshot)
        updates = ros.add(selected, updates=True)
        updates.get_nowait()
        fanout = TopicFanout(self.path)
        fanout.apply(ros.take_updates(updates))
        encoded = fanout.updates(selected, {})
        self.assertLess(len(encoded.encode()) + 256, MAX_RESPONSE)
        decoded = json.loads(encoded)
        self.assertEqual(sum(len(dumps(item["value"]["data"]).encode()) for item in decoded["updates"]), 262144)
        ros.remove(updates)
        ros.remove(channel)

    def test_updates_coalesce_each_topic_without_repeating_metadata_or_catalogs(self):
        self.ros.topics["other"] = dict(self.ros.topics["counter"], id="other")
        channel = self.ros.add(["counter", "other"], updates=True)
        channel.get_nowait()
        baseline = self.ros.take_updates(channel)
        self.assertTrue(baseline["reset"])
        self.assertEqual({item["id"] for item in baseline["metadata"]}, {"counter", "other"})
        self.assertNotIn("services", baseline)
        for key, sequence in (("counter", 1), ("other", 10), ("counter", 2)):
            self.ros.event(dict(kind="topic", generation=self.ros.generation, topic_id=key,
                run_id=self.run["run_id"], sequence=sequence, status="current", data=dict(data=str(sequence)),
                received_monotonic=self.clock(), stream_generation=key))
        self.assertEqual(channel.qsize(), 1)
        channel.get_nowait()
        value = self.ros.take_updates(channel)
        self.assertFalse(value["reset"])
        self.assertEqual(value["metadata"], [])
        self.assertEqual({item["id"]: item["sequence"] for item in value["topics"]}, {"counter": 2, "other": 10})
        self.assertTrue(all("fields" not in item and "projection" not in item for item in value["topics"]))
        self.ros.publish({"other"})
        channel.get_nowait()
        self.assertEqual([item["id"] for item in self.ros.take_updates(channel)["topics"]], ["other"])
        self.ros.remove(channel)
        self.assertFalse(self.ros.update_listeners)
        self.assertFalse(self.ros.bindings)
    def test_ipc_response_boundary_includes_envelope_and_newline(self):
        handler = object.__new__(Handler)
        handler.wfile = io.BytesIO()
        value = dict(ok=True, result="")
        value["result"] = "x" * (MAX_RESPONSE - len((dumps(value) + "\n").encode()))
        handler.send(value)
        raw = handler.wfile.getvalue()
        self.assertEqual(len(raw), 524288)
        self.assertEqual(decode_response(raw), value["result"])
        value["result"] += "x"
        self.assertFault("response_limit", lambda: handler.send(value))
        self.assertFault("transport_unavailable", lambda: decode_response((dumps(value) + "\n").encode()))

    def test_lossless_scalar_encoding_and_strict_request_types(self):
        maximum = 2**63-1
        self.assertEqual(scalar(maximum, "int64"), str(maximum))
        self.assertEqual(arguments(dict(data=str(maximum)), dict(data="int64")), dict(data=maximum))
        for value in (maximum, str(2**63), "01", "-0", True):
            self.assertFault("invalid_arguments", lambda value=value: arguments(dict(data=value), dict(data="int64")))
        self.assertFault("invalid_arguments", lambda: scalar(float("nan"), "double"))
        self.assertFault("invalid_arguments", lambda: scalar(10**400, "double"))
        self.assertFault("unsupported_type", lambda: scalar([], "sequence<int64>"))
        class Message:
            data = maximum
            hidden = "not exposed"
            def get_fields_and_field_types(self):
                return dict(data="int64", hidden="string")
        self.assertEqual(projection(Message(), selected_schema(Message(), ["data"])), dict(data=str(maximum)))
        self.assertFault("invalid_ros_profile", lambda: selected_schema(Message(), ["data"], complete=True))

    def test_watchers_share_demand_and_last_removal_discards_samples(self):
        first, second = self.ros.add(["counter"]), self.ros.add(["counter"])
        self.assertEqual(len(self.ros.transport.messages), 1)
        self.ros.event(dict(kind="topic", generation=self.ros.generation, topic_id="counter", run_id=self.run["run_id"],
                            status="current", data=dict(data="9007199254740993"), received_monotonic=self.clock(), stream_generation="sample-one"))
        self.assertTrue(self.ros.snapshot()["topics"][0]["fresh"])
        self.clock.now += 2
        self.assertEqual(self.ros.snapshot()["topics"][0]["status"], "stale")
        self.ros.remove(first)
        self.assertEqual(len(self.ros.transport.messages), 1)
        self.ros.remove(second)
        self.assertEqual(self.ros.transport.messages[-1]["bindings"], {})
        self.assertFalse(self.ros.snapshot()["topics"][0]["fresh"])
        self.assertFalse(self.ros.samples)

    def test_service_intent_deduplicates_and_rejects_stale_generation(self):
        command = self.command()
        stale = dict(command, generation="old")
        self.assertFault("stale_generation", lambda: self.core.handle(stale))
        operation = self.core.handle(command)
        self.assertEqual(operation["phase"], "prepared")
        self.assertEqual(self.core.handle(command), operation)
        self.assertEqual(len(self.ros.transport.messages), 1)
        self.assertFault("request_conflict", lambda: self.core.handle(dict(command, arguments=dict(data=False))))
        self.assertFault("busy", lambda: self.core.handle(self.command()))
        self.assertNotIn("arguments", self.store.operation(operation["request_id"]))

    def test_receipt_cannot_refresh_old_acquisition_and_progress_ages_independently(self):
        self.ros.add(["counter"])
        self.ros.event(dict(kind="topic", generation=self.ros.generation, topic_id="counter", run_id=self.run["run_id"],
                            status="current", data=dict(acquisition_timestamp_ns="1791560000123456789"),
                            received_monotonic=self.clock(), acquisition_age_at_receipt=2.0,
                            progress_monotonic=self.clock() - 3))
        value = self.ros.snapshot()["topics"][0]
        self.assertEqual(value["age_seconds"], 0)
        self.assertFalse(value["fresh"])
        self.assertEqual(value["status"], "stale_source")
        self.assertEqual(value["progress_age_seconds"], 3)
        self.assertEqual(value["acquisition_timestamp"], "1791560000123456789")
        self.clock.now += 0.5
        self.assertEqual(self.ros.snapshot()["topics"][0]["acquisition_age_seconds"], 2.5)

    def test_worker_authorization_reaudits_ownership_and_expiry(self):
        operation = self.core.handle(self.command())
        self.supervisor.services[self.run["name"]]["InvocationID"] = uuid.uuid4().hex
        self.assertFault("precondition", lambda: self.authorize(operation))
        self.assertEqual(self.ros.transport.messages[0]["kind"], "call")
        self.assertEqual(self.core.operations[operation["request_id"]]["phase"], "prepared")

    def test_estimated_sample_time_expires_at_the_boundary_despite_fresh_receipts(self):
        self.ros.add(["counter"])
        self.ros.topics["counter"]["stale_after_seconds"] = .1
        self.ros.event(dict(kind="topic", generation=self.ros.generation, topic_id="counter", run_id=self.run["run_id"],
                           status="current", data=dict(sample_timestamp_ns="1791637000123456789"),
                           received_monotonic=self.clock(), sample_age_at_receipt=.1))
        value = self.ros.snapshot()["topics"][0]
        self.assertEqual(value["age_seconds"], 0)
        self.assertEqual(value["sample_age_seconds"], .1)
        self.assertIsNone(value["acquisition_age_seconds"])
        self.assertFalse(value["fresh"])
        self.assertEqual(value["status"], "stale_source")

    def test_provider_refusal_is_a_correlated_complete_response(self):
        operation = self.core.handle(self.command())
        self.authorize(operation)
        self.ros.event(dict(kind="operation", generation=self.ros.generation, request_id=operation["request_id"],
                           phase="complete", delivery="responded", outcome="provider_rejected", response=dict(success=False, message="fixture refused")))
        saved = self.core.handle(dict(version=1, action="operation", request_id=operation["request_id"]))
        self.assertEqual(saved["phase"], "complete")
        self.assertFalse(saved["response"]["success"])
        self.assertIsNone(self.ros.active)

    def test_timeout_requires_local_acknowledgement_and_never_replays(self):
        operation = self.core.handle(self.command())
        self.authorize(operation)
        self.clock.now += 3
        self.ros.tick()
        saved = self.core.operations[operation["request_id"]]
        self.assertEqual(saved["phase"], "timed_out")
        self.assertFault("outcome_unknown", lambda: self.core.handle(self.command()))
        self.ros.event(dict(kind="operation", generation=self.ros.generation, request_id=operation["request_id"], phase="complete", delivery="responded", outcome="provider_accepted"))
        self.assertEqual(saved["phase"], "timed_out")
        self.core.handle(dict(version=1, action="ros_acknowledge", request_id=operation["request_id"], expected_run=self.run["run_id"]))
        self.assertEqual(saved["phase"], "timed_out")
        self.assertEqual(len([m for m in self.ros.transport.messages if m["kind"] == "call"]), 1)
        self.core.handle(self.command())
        self.assertEqual(len([m for m in self.ros.transport.messages if m["kind"] == "call"]), 2)

    def test_manager_and_worker_recovery_preserve_unknown_intent(self):
        command = self.command()
        operation = self.core.handle(command)
        restored = RosRuntime(self.core)
        self.assertEqual(self.core.operations[operation["request_id"]]["phase"], "outcome_unknown")
        self.assertIsNone(restored.active)
        self.ros.lost("test worker loss")
        self.assertFalse(self.ros.snapshot()["available"])
        self.ros.generation = "replacement"
        self.ros.event(dict(kind="status", generation="ros-generation", ready=True, schemas={}, subscriptions=[]))
        self.assertFalse(self.ros.ready)
        # Same retained command returns uncertainty even after expiry/recovery.
        self.clock.now += 40
        self.assertEqual(self.core.handle(command)["phase"], "outcome_unknown")

    def test_service_discovery_is_separate_from_interface_readiness(self):
        self.assertTrue(self.ros.snapshot()["available"])
        self.assertFalse(self.ros.snapshot()["services"][0]["available"])
        self.ros.event(dict(kind="status", generation=self.ros.generation, ready=True, schemas=self.ros.schemas,
                            subscriptions=[], service_availability=dict(enable=dict(available=True, error=None))))
        self.assertTrue(self.ros.snapshot()["services"][0]["available"])
        revision = self.ros.summary()["capability_revision"]
        operation = self.core.handle(self.command())
        self.assertGreater(self.ros.summary()["capability_revision"], revision)
        self.ros.lost("test disconnect")
        self.assertFalse(self.ros.snapshot()["services"][0]["available"])
        self.assertEqual(self.core.operations[operation["request_id"]]["phase"], "outcome_unknown")

    def test_stop_invalidates_topics_and_pending_service_results(self):
        channel = self.ros.add(["counter"])
        operation = self.core.handle(self.command())
        self.run["stop_request_id"] = operation["request_id"]
        self.ros.runtime_changed()
        self.assertEqual(self.ros.bindings, {})
        self.ros.event(dict(kind="operation", generation=self.ros.generation, request_id=operation["request_id"], phase="complete", delivery="responded"))
        self.assertEqual(self.core.operations[operation["request_id"]]["phase"], "outcome_unknown")
        self.assertFalse(channel.get_nowait()["topics"][0]["fresh"])


if __name__ == "__main__":
    unittest.main()
