"""Fixed topic choices share durable command authority without replay."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import copy
import unittest
import uuid

from robot_runtime.profile import validate
from robot_runtime.common import Fault
from robot_runtime.ros_runtime import RosRuntime
import test_ros


class CommandTests(unittest.TestCase):
    tearDown = test_ros.RosTests.tearDown
    assertFault = test_ros.RosTests.assertFault
    authorize = test_ros.RosTests.authorize

    def setUp(self):
        test_ros.RosTests.setUp(self)
        self.config["ros"]["commands"] = [dict(id="state", label="State request", unit_id="worker", node="/fixture", name="/fixture/request",
            type="std_msgs/msg/UInt8", fields=["data"], timeout_seconds=2,
            choices=[dict(id="disable", label="Disable", arguments=dict(data=0)), dict(id="enable", label="Enable", arguments=dict(data=1))])]
        self.config, self.core.revision = validate(self.config, check_paths=False)
        self.core.profile = self.config
        self.ros = self.core.ros = RosRuntime(self.core)
        self.ros.transport = test_ros.Transport()
        self.ros.generation, self.ros.ready = "generation", True
        self.ros.schemas = dict(state=dict(request=dict(data="uint8")))
        self.ros.command_availability = dict(state=dict(available=True, error=None))

    def command(self, **changes):
        value = dict(version=1, action="ros_topic_command", capability_id="state", choice="disable", request_id=str(uuid.uuid4()),
            expected_run=self.run["run_id"], generation=self.ros.generation, config_revision=self.core.revision,
            expires_at_ms=int(self.clock.wall()*1000)+10000)
        return dict(value, **changes)

    def test_fixed_choices_deduplicate_and_keep_publication_separate(self):
        value = self.command()
        result = self.core.handle(value)
        self.assertEqual(result, self.core.handle(value))
        self.assertEqual(len(self.ros.transport.messages), 1)
        self.assertEqual(self.ros.transport.messages[0]["choice"], "disable")
        self.assertFault("request_conflict", lambda: self.core.handle(dict(value, choice="enable")))
        self.authorize(result)
        self.ros.event(dict(kind="operation", generation=self.ros.generation, request_id=result["request_id"],
            phase="complete", delivery="published", outcome="published_once", response=dict(application_acceptance="unconfirmed")))
        saved = self.core.operations[result["request_id"]]
        self.assertEqual(saved["response"]["application_acceptance"], "unconfirmed")
        self.assertEqual(saved["choice"], "disable")

    def test_rejects_arbitrary_input_and_stale_authority(self):
        for changes, code in ((dict(choice="arbitrary"), "invalid_arguments"), (dict(arguments=dict(data=2)), "invalid_request"),
            (dict(expected_run="old"), "stale_run"), (dict(generation="old"), "stale_generation"),
            (dict(config_revision="old"), "stale_config"), (dict(expires_at_ms=0), "expired")):
            self.assertFault(code, lambda: self.core.handle(self.command(**changes)))
        self.assertFalse(self.ros.transport.messages)
        result = self.core.handle(self.command())
        self.clock.now += 3
        self.assertFault("expired", lambda: self.authorize(result))

    def test_restart_never_replays_and_requires_local_uncertainty_acknowledgement(self):
        result = self.core.handle(self.command())
        self.ros = self.core.ros = RosRuntime(self.core)
        self.ros.transport, self.ros.generation, self.ros.ready = test_ros.Transport(), "new", True
        self.ros.schemas = dict(state=dict(request=dict(data="uint8")))
        self.assertFalse(self.ros.transport.messages)
        self.assertEqual(self.core.operations[result["request_id"]]["phase"], "outcome_unknown")
        self.assertFault("outcome_unknown", lambda: self.core.handle(self.command()))
        self.core.handle(dict(version=1, action="ros_acknowledge", request_id=result["request_id"], expected_run=self.run["run_id"]))
        self.core.handle(self.command())
        self.assertEqual(len(self.ros.transport.messages), 1)

    def test_profile_restricts_choices_and_aggregate_observation_size(self):
        for changes in (dict(type="std_msgs/srv/UInt8"), dict(choices=[]), dict(timeout_seconds=30),
                        dict(choices=[dict(id="bad", label="bad", arguments=dict(other=1))])):
            bad = copy.deepcopy(self.config)
            bad["ros"]["commands"][0].update(changes)
            with self.assertRaises(Fault): validate(bad, check_paths=False)
        bad = copy.deepcopy(self.config)
        spec = bad["ros"]["topics"][0]
        bad["ros"]["topics"] = [dict(spec, id="t"+str(i), max_json_bytes=16384) for i in range(17)]
        self.assertFault("invalid_ros_profile", lambda: validate(bad, check_paths=False))
