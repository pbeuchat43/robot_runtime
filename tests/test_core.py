"""Policy regressions with a supervisor fake; execute only on the Linux test host."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import copy
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from robot_runtime.common import Fault, loads
from robot_runtime.core import Core
from robot_runtime.profile import validate
from robot_runtime.store import Store
from robot_runtime.supervisor import expected_properties, launch_argv


class Clock:
    now = 10.0
    def __call__(self):
        return self.now
    def wall(self):
        return 1000 + self.now


def profile():
    return dict(schema_version=1, robot_id="fixture", namespace="test", units=[
        dict(id="worker", label="Worker", argv=["/usr/bin/python3", "-c", "pass"], working_directory="/tmp",
             environment={}, stop=dict(signal="SIGINT", timeout_seconds=2), public_logs=True)])


class Supervisor:
    def __init__(self, clock):
        self.clock = clock
        self.services = {}
        self.starts = 0
        self.signals = 0
        self.crash = None
        self.delay = 0

    def names(self):
        self.clock.now += self.delay
        return list(self.services)

    def snapshot(self, name, *, full=True):
        result = copy.deepcopy(self.services.get(name))
        if result:
            result["observed_monotonic"] = self.clock()
        return result

    def start(self, run):
        if self.crash == "before":
            raise OSError("simulated dispatch failure")
        self.starts += 1
        argv = launch_argv(run)
        self.services[run["name"]] = dict(expected_properties(run), Id=run["name"], Transient=True,
            InvocationID=uuid.uuid4().hex, Environment=["ROBOT_RUNTIME_RUN=" + run["run_id"]],
            ExecStart=[[argv[0], argv, False]], MainPID=44, ExecMainPID=44, ExecMainCode=0, ExecMainStatus=0,
            ActiveState="active", SubState="running", Result="success", members=[44], Job=[0, "/"],
            observed_monotonic=self.clock())
        if self.crash == "after":
            raise OSError("simulated lost reply")

    def signal(self, _run):
        self.signals += 1

    def retain(self, _name):
        pass

    def release(self, name, *, failed=False):
        self.services.pop(name)

    def exit(self, run, status=0):
        self.services[run["name"]].update(MainPID=0, members=[], ActiveState="inactive" if status == 0 else "failed",
                                          SubState="dead", Result="success" if status == 0 else "exit-code",
                                          ExecMainCode=1, ExecMainStatus=status)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name)
        os.chmod(self.path, 0o700)
        self.config, self.revision = validate(profile())
        self.clock = Clock()
        self.supervisor = Supervisor(self.clock)
        self.store = Store(self.path)
        self.core = Core(self.config, self.revision, self.store, "boot-one", clock=self.clock, wall=self.clock.wall)
        self.core.connected(self.supervisor)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def command(self, action="start", **values):
        return dict(version=1, action=action, unit_id="worker", request_id=str(uuid.uuid4()),
                    expires_at_ms=int(self.clock.wall() * 1000) + 10000, config_revision=self.revision, **values)

    def start(self):
        operation = self.core.handle(self.command())
        return self.core.runs[operation["run_id"]]

    def restart(self, boot="boot-one"):
        self.store.close()
        self.store = Store(self.path)
        self.core = Core(self.config, self.revision, self.store, boot, clock=self.clock, wall=self.clock.wall)
        self.core.connected(self.supervisor)

    def assertFault(self, code, function):
        with self.assertRaises(Fault) as caught:
            function()
        self.assertEqual(code, caught.exception.code)

    def test_explicit_start_deduplicates_and_rejects_conflicting_reuse(self):
        request = self.command()
        first = self.core.handle(request)
        self.clock.now += 60
        self.assertEqual(first, self.core.handle(request))
        self.assertEqual(self.supervisor.starts, 1)
        self.assertFault("request_conflict", lambda: self.core.handle(dict(request, expires_at_ms=request["expires_at_ms"] + 1)))

    def test_unavailable_catalog_entry_has_no_launch_and_rejects_start(self):
        disabled = dict(id="worker", label="Worker", available=False, unavailable_reason="Not integrated yet")
        source = profile()
        source["units"] = [disabled]
        self.config, self.revision = validate(source)
        self.restart()
        status = self.core.status()["units"][0]
        self.assertFalse(status["available"])
        self.assertEqual(status["unavailable_reason"], "Not integrated yet")
        self.assertFault("unit_unavailable", lambda: self.core.handle(self.command()))
        self.assertEqual(self.supervisor.starts, 0)
        self.assertFault("invalid_request", lambda: validate(dict(source, units=[dict(disabled, argv=["/bin/true"])])))
        self.assertFault("invalid_profile", lambda: validate(dict(source, units=[dict(disabled, available="false")])))

    def test_available_unit_cannot_require_an_unavailable_unit(self):
        source = profile()
        source["units"][0]["requires_running"] = ["later"]
        source["units"].append(dict(id="later", label="Later", available=False, unavailable_reason="Not integrated yet"))
        self.assertFault("invalid_profile", lambda: validate(source))

    def conflicting_units(self):
        source = profile()
        source["units"][0]["conflicts"] = ["peer"]
        source["units"][0]["launch_modes"] = [dict(id="images", label="Images", argv=["/usr/bin/python3", "-c", "pass"])]
        source["units"].append(dict(source["units"][0], id="peer", label="Peer", conflicts=["worker"]))
        self.config, self.revision = validate(source)
        self.restart()

    def test_conflict_blocks_all_modes_through_restart_and_stop_timeout(self):
        self.conflicting_units()
        worker = self.start()
        def peer_start(): return self.core.handle(dict(self.command(), unit_id="peer", mode="images"))
        self.assertFault("unit_conflict", peer_start)
        peer = next(unit for unit in self.core.status()["units"] if unit["unit_id"] == "peer")
        self.assertEqual(peer["start_blocked_by"], ["worker"])
        self.restart()
        self.assertFault("unit_conflict", peer_start)
        self.core.handle(self.command("stop", expected_run=worker["run_id"]))
        self.clock.now += 3
        self.core.audit()
        self.assertFault("unit_conflict", peer_start)
        self.assertEqual((self.supervisor.starts, self.supervisor.signals), (1, 1))
        self.supervisor.exit(worker)
        self.core.audit()
        operation = peer_start()
        self.assertEqual(operation["phase"], "complete")
        self.assertFault("unit_conflict", lambda: self.core.handle(self.command(mode="images")))
        self.assertEqual((self.supervisor.starts, self.supervisor.signals), (2, 1))

    def test_uncertain_dispatch_reserves_conflicting_unit_until_resolution(self):
        self.conflicting_units()
        self.supervisor.crash = "before"
        operation = self.core.handle(self.command())
        self.restart()
        self.assertFault("unit_conflict", lambda: self.core.handle(dict(self.command(), unit_id="peer")))
        self.core.handle(self.command("resolve", expected_run=operation["run_id"]))
        self.supervisor.crash = None
        result = self.core.handle(dict(self.command(), unit_id="peer"))
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(self.supervisor.starts, 1)

    def test_failure_and_success_do_not_restart_and_allow_a_new_explicit_run(self):
        for status in (7, 0):
            run = self.start()
            self.supervisor.exit(run, status)
            self.core.audit()
            self.assertFalse(run["blocked"])
            self.assertEqual(run["lifecycle"], "failed" if status else "exited")
        self.assertEqual(self.supervisor.starts, 2)

    def test_restart_keeps_owned_run_without_launch_or_signal(self):
        run = self.start()
        invocation = run["invocation"]
        self.restart()
        self.assertEqual(self.core.current("worker")["invocation"], invocation)
        self.assertEqual((self.supervisor.starts, self.supervisor.signals), (1, 0))

    def test_clean_signal_exit_preserves_raw_status_without_claiming_failure(self):
        run = self.start()
        self.supervisor.exit(run)
        self.supervisor.services[run["name"]].update(ExecMainCode=2, ExecMainStatus=15)
        self.core.audit()
        self.assertEqual(run["lifecycle"], "exited")
        self.assertEqual(self.core.status()["units"][0]["main_exit_status"], 15)

    def test_lost_start_reply_is_recovered_without_replay(self):
        self.supervisor.crash = "after"
        operation = self.core.handle(self.command())
        self.assertEqual(operation["phase"], "outcome_unknown")
        self.restart()
        self.assertEqual(self.supervisor.starts, 1)
        self.assertEqual(self.core.operations[operation["request_id"]]["phase"], "complete")

    def test_undispatched_intent_stays_unknown_until_explicit_resolution(self):
        self.supervisor.crash = "before"
        operation = self.core.handle(self.command())
        self.restart()
        run = self.core.current("worker")
        self.assertEqual(run["lifecycle"], "outcome_unknown")
        self.assertFault("busy", lambda: self.core.handle(self.command()))
        result = self.core.handle(self.command("resolve", expected_run=run["run_id"]))
        self.assertEqual(result["outcome"], "absence_resolved")
        self.assertEqual(self.core.operations[operation["request_id"]]["phase"], "outcome_unknown")
        self.assertEqual(self.supervisor.starts, 0)

    def test_stop_timeout_preserves_process_and_is_not_repeated_on_restart(self):
        run = self.start()
        operation = self.core.handle(self.command("stop", expected_run=run["run_id"]))
        self.clock.now += 3
        self.restart()
        self.assertEqual(self.core.operations[operation["request_id"]]["phase"], "timed_out")
        self.assertTrue(self.core.current("worker")["blocked"])
        self.assertFault("stop_already_requested", lambda: self.core.handle(self.command("stop", expected_run=run["run_id"])))
        self.assertEqual(self.supervisor.signals, 1)
        self.supervisor.exit(run)
        self.core.audit()
        self.assertEqual(self.core.operations[operation["request_id"]]["phase"], "timed_out")

    def test_stop_deadline_advances_while_supervisor_is_disconnected(self):
        run = self.start()
        operation = self.core.handle(self.command("stop", expected_run=run["run_id"]))
        self.core.disconnected()
        self.clock.now += 3
        self.core.progress()
        self.assertEqual(self.core.operations[operation["request_id"]]["phase"], "timed_out")
        self.assertEqual(self.supervisor.signals, 1)
        self.assertFalse(self.core.needs_progress())

    def test_saved_terminal_evidence_finishes_stop_after_crash(self):
        run = self.start()
        operation = self.core.handle(self.command("stop", expected_run=run["run_id"]))
        self.clock.now += 1
        self.supervisor.exit(run)
        self.core._observe(run, full=True)  # Crash after saving exit, before updating the operation.
        self.clock.now += 3
        self.restart()
        self.assertEqual(self.core.operations[operation["request_id"]]["phase"], "complete")

    def test_first_empty_observation_after_deadline_preserves_timeout(self):
        run = self.start()
        operation = self.core.handle(self.command("stop", expected_run=run["run_id"]))
        self.clock.now += 3
        self.supervisor.exit(run)
        self.core.audit()
        self.assertEqual(self.core.operations[operation["request_id"]]["phase"], "timed_out")
        self.assertFalse(self.core.current("worker")["blocked"])

    def test_stale_stop_and_changed_ownership_never_signal(self):
        run = self.start()
        self.assertFault("stale_run", lambda: self.core.handle(self.command("stop", expected_run="old")))
        self.supervisor.services[run["name"]]["InvocationID"] = uuid.uuid4().hex
        self.assertFault("ownership_conflict", lambda: self.core.handle(self.command("stop", expected_run=run["run_id"])))
        self.assertEqual(self.supervisor.signals, 0)

    def test_foreign_namespace_blocks_commands(self):
        self.supervisor.services["foreign.service"] = {}
        self.assertFault("ownership_conflict", lambda: self.core.handle(self.command()))
        self.assertEqual(self.supervisor.starts, 0)

    def test_foreign_service_does_not_prevent_stopping_an_owned_run(self):
        run = self.start()
        self.supervisor.services["foreign.service"] = {}
        self.core.handle(self.command("stop", expected_run=run["run_id"]))
        self.assertEqual(self.supervisor.signals, 1)

    def test_local_inspection_exposes_identity_without_launch_arguments(self):
        run = self.start()
        value = self.core.handle(dict(version=1, action="inspect", run_id=run["run_id"]))
        self.assertEqual(value["name"], run["name"])
        self.assertEqual(value["invocation"], run["invocation"])
        self.assertNotIn("spec", value)

    def test_descendants_block_replacement_after_main_exit(self):
        run = self.start()
        self.supervisor.services[run["name"]]["MainPID"] = 0
        self.core.audit()
        self.assertEqual(run["lifecycle"], "descendants_surviving")
        self.assertFault("busy", lambda: self.core.handle(self.command()))

    def test_expiry_is_checked_again_after_audit(self):
        self.supervisor.delay = 11
        self.assertFault("expired", lambda: self.core.handle(self.command()))
        self.assertEqual(self.supervisor.starts, 0)

    def test_unknown_status_is_never_refreshed_by_a_cached_read(self):
        self.start()
        self.clock.now += 16
        self.assertFalse(self.core.status()["fresh"])
        self.core.disconnected()
        self.assertFalse(self.core.status()["fresh"])

    def test_reboot_does_not_replay_old_run_or_stop(self):
        run = self.start()
        self.supervisor.services.clear()
        self.restart("boot-two")
        self.assertEqual(self.core.current("worker")["lifecycle"], "interrupted_by_reboot")
        self.assertFalse(self.core.current("worker")["blocked"])
        self.assertEqual(self.supervisor.starts, 1)

    def test_public_status_omits_environment_and_executable(self):
        self.start()
        encoded = json.dumps(self.core.status())
        self.assertNotIn("/usr/bin/python3", encoded)
        self.assertNotIn("ROBOT_RUNTIME_RUN", encoded)

    def test_database_lock_and_live_unit_constraint_preserve_existing_run(self):
        self.assertFault("manager_running", lambda: Store(self.path))
        run = self.start()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save(run=dict(run, run_id=uuid.uuid4().hex))
        self.assertEqual(len(self.store.runs()), 1)


class ProfileTests(unittest.TestCase):
    def test_conflicts_require_unique_reciprocal_available_units(self):
        source = profile()
        source["units"][0]["conflicts"] = ["peer"]
        source["units"].append(dict(source["units"][0], id="peer", label="Peer", conflicts=["worker"]))
        validate(source)
        for change in (
            lambda v: v["units"][0].update(conflicts="peer"),
            lambda v: v["units"][0].update(conflicts=["peer", "peer"]),
            lambda v: v["units"][0].update(conflicts=["worker"]),
            lambda v: v["units"][0].update(conflicts=["missing"]),
            lambda v: v["units"][1].update(conflicts=[]),
            lambda v: v["units"][0].update(requires_running=["peer"]),
            lambda v: v["units"].__setitem__(1, dict(id="peer", label="Peer", available=False, unavailable_reason="Later")),
        ):
            bad = copy.deepcopy(source)
            change(bad)
            with self.assertRaises(Fault): validate(bad)

    def test_rejects_shell_like_paths_reserved_environment_and_bad_deadlines(self):
        for mutate in (
            lambda v: v["units"][0].update(argv=["echo hi"]),
            lambda v: v["units"][0].update(environment={"ROBOT_RUNTIME_RUN": "fake"}),
            lambda v: v["units"][0]["stop"].update(timeout_seconds=float("nan")),
            lambda v: v["units"][0].update(requires_running=[["bad"]]),
            lambda v: v["units"].append(copy.deepcopy(v["units"][0])),
            lambda v: v["units"][0].update(requires_running=["worker"]),
        ):
            with self.subTest(mutate=mutate):
                value = profile()
                mutate(value)
                with self.assertRaises(Fault):
                    validate(value)

    def test_json_rejects_duplicate_fields_and_nonfinite_numbers(self):
        for value in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'):
            with self.assertRaises(Fault):
                loads(value)


if __name__ == "__main__":
    unittest.main()
