"""Parameter wire, exposure, freshness and durable command policy checks."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import copy
from types import SimpleNamespace
import unittest
import uuid

from robot_runtime.common import Fault
from robot_runtime.parameter_codec import changes, descriptor, from_message, typed
from robot_runtime.parameter_runtime import ParameterRuntime
from robot_runtime.profile import validate
from robot_runtime.ros_runtime import RosRuntime
import test_ros


def spec():
    return dict(id="settings", label="Provider settings", unit_id="worker", node="/fixture",
        parameters=[dict(name="rate", write=True), dict(name="identity", write=False)],
        timeout_seconds=3, stale_after_seconds=10)


def parameters():
    meta = dict(declared_type="integer", description="Rate", additional_constraints="", read_only=False,
                dynamic_typing=False, integer_range=[], floating_point_range=[])
    return dict(rate=dict(type="integer", value="20", descriptor=meta, writable=True),
                identity=dict(type="integer", value="9007199254740993", descriptor=dict(meta, read_only=True), writable=False))


class ParameterTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_ros.RosTests("runTest")
        self.fixture.setUp()
        self.ros, self.core, self.clock = self.fixture.ros, self.fixture.core, self.fixture.clock
        self.ros.config["parameters"] = [spec()]
        self.ros.parameters = ParameterRuntime(self.ros)
        self.ros.parameters.availability = dict(settings=dict(available=True, writable=True))

    def tearDown(self):
        self.fixture.tearDown()

    def read(self, data=None):
        self.clock.now += 1.1
        value = self.core.handle(dict(version=1, action="ros_parameters", capability_id="settings", refresh=True))
        self.ros.event(dict(kind="parameter_read", generation=self.ros.generation, request_id=value["read_id"],
            parameters=parameters() if data is None else data, received_monotonic=self.clock()))
        return self.ros.parameters.snapshot("settings")

    def command(self, **values):
        body = dict(version=1, action="ros_parameter_set", capability_id="settings", request_id=str(uuid.uuid4()),
            expected_run=self.fixture.run["run_id"], generation=self.ros.generation, config_revision=self.core.revision,
            expires_at_ms=int(self.clock.wall()*1000)+10000,
            expected_snapshot=self.ros.parameters.snapshot("settings")["snapshot_id"],
            changes=dict(rate=dict(type="integer", value="25")))
        body.update(values)
        return body

    def fault(self, code, function):
        with self.assertRaises(Fault) as caught:
            function()
        self.assertEqual(caught.exception.code, code)

    def complete(self, operation, accepted=True):
        self.ros.event(dict(kind="operation", generation=self.ros.generation, request_id=operation["request_id"],
            phase="complete", delivery="responded", outcome="provider_accepted" if accepted else "provider_rejected",
            response=dict(successful=accepted, reason="fixture", reason_truncated=False),
            readback=dict(status="pending" if accepted else "not_requested")))

    def test_parameter_types_are_lossless_and_bounded(self):
        for kind, wire, native in (("bool", True, True), ("integer", "9223372036854775807", 2**63-1),
            ("double", 1.25, 1.25), ("string", "hello", "hello"), ("byte_array", [0,255], [0,255]),
            ("bool_array", [True,False], [True,False]), ("integer_array", ["-9223372036854775808","9007199254740993"], [-2**63,2**53+1]),
            ("double_array", [0.25,1.5], [0.25,1.5]), ("string_array", ["", "x"], ["", "x"])):
            self.assertEqual(typed(dict(type=kind,value=wire),to_ros=True)["value"],native)
            self.assertEqual(typed(dict(type=kind,value=native))["value"],wire)
        self.assertEqual(from_message(SimpleNamespace(type=5, byte_array_value=[b"\x00",b"\xff"]))["value"],[0,255])
        for item in (dict(type="integer",value=9007199254740993), dict(type="integer",value="01"),
                     dict(type="bool",value=1),dict(type="double",value=float("inf")),
                     dict(type="byte_array",value=[256]),dict(type="bool_array",value=[0]),dict(type="not_set",value=None)):
            self.fault("invalid_arguments",lambda item=item:typed(item,to_ros=True))
        self.fault("parameter_limit",lambda:typed(dict(type="integer_array",value=["0"]*129),to_ros=True))

    def test_descriptors_preserve_ranges_and_read_only_information(self):
        value=descriptor(SimpleNamespace(name="rate",type=2,description="sample rate",additional_constraints="even values",
            read_only=True,dynamic_typing=False,integer_range=[SimpleNamespace(from_value=-2**63,to_value=2**63-1,step=2**64-1)],
            floating_point_range=[]),"rate")
        self.assertTrue(value["read_only"])
        self.assertEqual(value["integer_range"][0]["step"],str(2**64-1))
        self.assertEqual(value["additional_constraints"],"even values")

    def test_profile_requires_explicit_bounded_parameter_permissions(self):
        validate(self.fixture.config,check_paths=False)
        for parameters_list in ([], [dict(name="*",write=True)], [dict(name="rate",write="yes")],
                                [dict(name="rate",write=True)]*2):
            bad=copy.deepcopy(self.fixture.config)
            bad["ros"]["parameters"][0]["parameters"]=parameters_list
            self.fault("invalid_ros_profile",lambda:validate(bad,check_paths=False))

    def test_twelve_parameter_groups_preserve_per_group_bounds(self):
        value = copy.deepcopy(self.fixture.config)
        value["ros"]["parameters"] = [dict(spec(), id="settings" + str(index)) for index in range(12)]
        validate(value, check_paths=False)
        value["ros"]["parameters"].append(dict(spec(), id="settings12"))
        self.fault("invalid_ros_profile", lambda: validate(value, check_paths=False))

    def test_reads_coalesce_and_age_without_mutation_history(self):
        before=len(self.core.operations)
        value=dict(version=1,action="ros_parameters",capability_id="settings",refresh=True)
        first=self.core.handle(value)
        second=self.core.handle(value)
        self.assertEqual(first["read_id"],second["read_id"])
        self.assertEqual(len(self.ros.transport.messages),1)
        self.ros.event(dict(kind="parameter_read",generation=self.ros.generation,request_id=first["read_id"],
            parameters=parameters(),received_monotonic=self.clock()))
        current=self.ros.parameters.snapshot("settings")
        self.assertTrue(current["fresh"])
        self.assertEqual(self.read()["snapshot_id"],current["snapshot_id"])
        self.clock.now+=11
        self.assertFalse(self.ros.parameters.snapshot("settings")["fresh"])
        self.assertEqual(len(self.core.operations),before)

    def test_write_requires_fresh_snapshot_exact_type_and_permission(self):
        self.read()
        self.fault("stale_snapshot",lambda:self.core.handle(self.command(expected_snapshot="old")))
        self.fault("read_only",lambda:self.core.handle(self.command(changes=dict(identity=dict(type="integer",value="1")))))
        self.fault("not_found",lambda:self.core.handle(self.command(changes=dict(hidden=dict(type="integer",value="1")))))
        self.fault("invalid_arguments",lambda:self.core.handle(self.command(changes=dict(rate=dict(type="double",value=20.5)))))
        self.clock.now+=11
        self.fault("stale_snapshot",lambda:self.core.handle(self.command()))

    def test_mutations_deduplicate_and_invalidate_other_client_snapshots(self):
        self.read()
        first=self.command()
        second=self.command()
        operation=self.core.handle(first)
        self.assertEqual(self.core.handle(first),operation)
        self.fault("busy",lambda:self.core.handle(second))
        self.complete(operation,accepted=False)
        self.fault("stale_snapshot",lambda:self.core.handle(second))
        self.assertNotIn("changes",self.fixture.store.operation(operation["request_id"]))
        self.assertEqual(len([m for m in self.ros.transport.messages if m["kind"]=="parameter_set"]),1)

    def test_acceptance_and_readback_are_separate_durable_observations(self):
        self.read()
        operation=self.core.handle(self.command())
        self.complete(operation)
        saved=self.core.operations[operation["request_id"]]
        self.assertEqual(saved["phase"],"complete")
        self.assertEqual(saved["readback"]["status"],"pending")
        self.assertEqual(self.ros.active,operation["request_id"])
        self.ros.event(dict(kind="parameter_readback",generation=self.ros.generation,request_id=operation["request_id"],
            readback=dict(status="observed",matches_requested=False,parameters=dict(rate=dict(type="integer",value="30")))))
        self.assertEqual(saved["outcome"],"provider_accepted")
        self.assertFalse(saved["readback"]["matches_requested"])
        self.assertIsNone(self.ros.active)

    def test_lost_readback_does_not_erase_known_provider_acceptance(self):
        self.read()
        operation=self.core.handle(self.command())
        self.complete(operation)
        self.ros.lost("worker died")
        saved=self.core.operations[operation["request_id"]]
        self.assertEqual(saved["outcome"],"provider_accepted")
        self.assertEqual(saved["phase"],"complete")
        self.assertEqual(saved["readback"]["status"],"unavailable")
        self.assertIsNone(self.ros.unresolved(self.fixture.run["run_id"]))

    def test_stop_before_acknowledgement_preserves_received_provider_result(self):
        self.read()
        operation = self.core.handle(self.command())
        self.core.handle(dict(version=1, action="stop", unit_id="worker", request_id=str(uuid.uuid4()),
            config_revision=self.core.revision, expected_run=self.fixture.run["run_id"],
            expires_at_ms=int(self.clock.wall()*1000)+10000))
        self.complete(operation)
        saved = self.core.operations[operation["request_id"]]
        self.assertEqual(saved["outcome"], "provider_accepted")
        self.assertEqual(saved["phase"], "complete")
        self.assertEqual(saved["readback"]["error"], "runtime_changed")

    def test_mutation_timeout_blocks_other_ros_commands_until_acknowledged(self):
        self.read()
        operation=self.core.handle(self.command())
        self.clock.now+=4
        self.ros.tick()
        self.assertEqual(self.core.operations[operation["request_id"]]["phase"],"timed_out")
        self.fault("outcome_unknown",lambda:self.core.handle(self.fixture.command()))
        self.core.handle(dict(version=1,action="ros_acknowledge",request_id=operation["request_id"],expected_run=self.fixture.run["run_id"]))
        self.core.handle(self.fixture.command())

    def test_manager_restart_does_not_replay_mutations_or_pending_readback(self):
        self.read()
        operation=self.core.handle(self.command())
        restored=RosRuntime(self.core)
        self.assertEqual(self.core.operations[operation["request_id"]]["phase"],"outcome_unknown")
        self.assertIsNone(restored.active)
        self.ros.active=None
        self.core.operations[operation["request_id"]]["uncertainty_acknowledged"]=True
        self.read()
        next_operation=self.core.handle(self.command())
        self.complete(next_operation)
        RosRuntime(self.core)
        saved=self.core.operations[next_operation["request_id"]]
        self.assertEqual(saved["phase"],"complete")
        self.assertEqual(saved["readback"]["error"],"manager_restarted")

    def test_read_reply_for_an_old_run_is_not_presented_as_current(self):
        value=self.core.handle(dict(version=1,action="ros_parameters",capability_id="settings",refresh=True))
        self.core.handle(dict(version=1, action="stop", unit_id="worker", request_id=str(uuid.uuid4()),
            config_revision=self.core.revision, expected_run=self.fixture.run["run_id"],
            expires_at_ms=int(self.clock.wall()*1000)+10000))
        self.ros.event(dict(kind="parameter_read",generation=self.ros.generation,request_id=value["read_id"],
            parameters=parameters(),received_monotonic=self.clock()))
        result=self.ros.parameters.snapshot("settings")
        self.assertEqual(result["error"],"runtime_changed")
        self.assertFalse(result["parameters"])
        self.fault("precondition",lambda:self.core.handle(dict(version=1,action="ros_parameters",capability_id="settings",refresh=True)))


if __name__ == "__main__":
    unittest.main()
