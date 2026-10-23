"""Action reservations and recovery policy without a native ROS participant."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import copy
import unittest
import uuid
from unittest.mock import patch

from robot_runtime.profile import validate
from robot_runtime.ros_runtime import RosRuntime
import test_ros


class ActionTests(unittest.TestCase):
    tearDown = test_ros.RosTests.tearDown
    assertFault = test_ros.RosTests.assertFault

    def setUp(self):
        test_ros.RosTests.setUp(self)
        spec = dict(id="count", label="Count", unit_id="worker", node="/fixture", name="/fixture/count",
                    type="fixture/action/Count", goal_fields=["steps"], result_fields=["completed"], feedback_fields=["completed"],
                    response_timeout_seconds=2, max_feedback_rate_hz=2, stale_after_seconds=1)
        self.config["ros"]["actions"] = [spec]
        self.config, self.core.revision = validate(self.config, check_paths=False)
        self.core.profile = self.config
        self.ros = self.core.ros = RosRuntime(self.core)
        self.ros.transport = test_ros.Transport()
        self.ros.generation, self.ros.ready = "generation", True
        self.ros.schemas = dict(count=dict(goal_schema=dict(steps="int64"), result_schema=dict(completed="int64"), feedback_schema=dict(completed="int64")))
        self.ros.actions.availability = dict(count=dict(available=True, error=None))

    def command(self, **changes):
        value = dict(version=1, action="ros_action_goal", capability_id="count", request_id=str(uuid.uuid4()),
                     expected_run=self.run["run_id"], generation=self.ros.generation, config_revision=self.core.revision,
                     expires_at_ms=int(self.clock.wall()*1000)+10000, arguments=dict(steps="9007199254740993"))
        value.update(changes)
        if value["action"] == "ros_action_cancel":
            value.pop("arguments")
        return value

    def event(self, rid, **values):
        self.ros.event(dict(kind="action_update", generation=self.ros.generation, request_id=rid, **values))

    def test_profile_limits_and_lossless_goal_arguments(self):
        for key, value in (("max_feedback_rate_hz", 6), ("response_timeout_seconds", 11), ("type", "fixture/srv/Count"), ("unit_id", "unknown")):
            bad = copy.deepcopy(self.config)
            bad["ros"]["actions"][0][key] = value
            self.assertFault("invalid_ros_profile", lambda: validate(bad, check_paths=False))
        self.assertFault("invalid_arguments", lambda: self.core.handle(self.command(arguments=dict(steps=2**53+1))))
        self.core.handle(self.command())
        self.assertEqual(self.ros.transport.messages[-1]["arguments"]["steps"], "9007199254740993")

    def test_duplicate_goal_is_never_resent_and_uncertainty_reserves_slot(self):
        command = self.command()
        goal = self.core.handle(command)
        self.assertEqual(self.core.handle(command), goal)
        self.assertEqual(len(self.ros.transport.messages), 1)
        self.assertFault("request_conflict", lambda: self.core.handle(dict(command, arguments=dict(steps="2"))))
        self.clock.now += 3
        self.ros.tick()
        self.assertEqual(self.core.operations[goal["request_id"]]["phase"], "timed_out")
        self.assertFault("busy", lambda: self.core.handle(self.command()))
        self.assertFault("not_found", lambda: self.core.handle(dict(version=1, action="ros_acknowledge", request_id=goal["request_id"], expected_run=self.run["run_id"])))
        self.assertNotIn("arguments", self.store.operation(goal["request_id"]))

    def test_cancellation_is_exact_and_acknowledgement_is_not_terminal(self):
        goal = self.core.handle(self.command())
        self.event(goal["request_id"], accepted=True, phase="in_progress", goal_status="executing")
        self.assertFault("stale_goal", lambda: self.core.handle(self.command(action="ros_action_cancel", goal_request_id=str(uuid.uuid4()))))
        cancel = self.core.handle(self.command(action="ros_action_cancel", goal_request_id=goal["request_id"]))
        self.assertFault("busy", lambda: self.core.handle(self.command(action="ros_action_cancel", goal_request_id=goal["request_id"])))
        self.ros.event(dict(kind="action_cancel_result", generation=self.ros.generation, request_id=cancel["request_id"], phase="complete", cancel_accepted=True))
        self.assertFalse(self.core.operations[goal["request_id"]]["terminal"])
        self.assertFault("busy", lambda: self.core.handle(self.command()))
        self.event(goal["request_id"], terminal=True, phase="complete", goal_status="canceled")
        self.assertTrue(self.ros.actions.catalog()[0]["can_send"])

    def test_authorization_rechecks_owner_expiry_and_generation(self):
        goal = self.core.handle(self.command())
        auth = dict(version=1, action="ros_action_authorize", generation=self.ros.generation, request_id=goal["request_id"])
        self.assertFault("stale_generation", lambda: self.core.handle(dict(auth, generation="old")))
        self.supervisor.services[self.run["name"]]["InvocationID"] = uuid.uuid4().hex
        self.assertFault("precondition", lambda: self.core.handle(auth))

    def test_restart_tracks_original_uuid_without_sending_goal_or_cancel(self):
        goal = self.core.handle(self.command())
        restored = self.core.ros = RosRuntime(self.core)
        restored.transport, restored.generation, restored.ready = test_ros.Transport(), "new", True
        restored.actions.sync()
        self.assertEqual(restored.transport.messages, [dict(kind="action_track", goals=dict(count=dict(request_id=goal["request_id"], run_id=self.run["run_id"])))])
        self.assertEqual(self.core.operations[goal["request_id"]]["phase"], "outcome_unknown")
        self.assertFault("busy", lambda: self.core.handle(self.command(generation="new")))

    def test_feedback_is_ephemeral_expires_and_old_generation_cannot_refresh_it(self):
        goal = self.core.handle(self.command())
        feedback = dict(kind="action_feedback", generation=self.ros.generation, request_id=goal["request_id"], data=dict(completed="1"),
                        received_monotonic=self.clock(), sequence=1, error=None)
        with patch.object(self.store, "save", wraps=self.store.save) as save:
            self.ros.event(feedback)
            save.assert_not_called()
        self.assertTrue(self.ros.actions.catalog()[0]["feedback_fresh"])
        self.clock.now += 2
        self.ros.event(dict(feedback, generation="old", received_monotonic=self.clock()))
        self.assertFalse(self.ros.actions.catalog()[0]["feedback_fresh"])
        self.ros.lost("lost")
        self.assertIsNone(self.ros.actions.catalog()[0]["feedback"])

    def test_new_goal_releases_previous_feedback_and_expired_dispatch_is_denied(self):
        first = self.core.handle(self.command())
        self.ros.actions.feedback[first["request_id"]] = dict(data=dict(completed="1"), received_monotonic=self.clock(), sequence=1, error=None)
        self.event(first["request_id"], terminal=True, phase="complete", goal_status="succeeded")
        second = self.core.handle(self.command())
        self.assertFalse(self.ros.actions.feedback)
        self.clock.now += 3
        self.assertFault("expired", lambda: self.core.handle(dict(version=1, action="ros_action_authorize", generation=self.ros.generation, request_id=second["request_id"])))

    def test_recovery_uses_goal_order_despite_a_backward_wall_clock_adjustment(self):
        first = self.core.handle(self.command())
        self.event(first["request_id"], terminal=True, phase="complete", goal_status="succeeded")
        self.clock.now -= 1
        second = self.core.handle(self.command())
        restored = RosRuntime(self.core)
        self.assertEqual(restored.actions.goal("count")["request_id"], second["request_id"])


if __name__ == "__main__":
    unittest.main()
