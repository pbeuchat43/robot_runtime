"""Owned-container races and aggregate policy, without a daemon or cameras."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import copy
import hashlib
import unittest

from robot_runtime.common import Fault
from robot_runtime.core import Core
from robot_runtime.docker import OwnedDocker
from robot_runtime.profile import validate
from test_core import PolicyTests, profile


class QueuedDocker(OwnedDocker):
    def __init__(self, core):
        super().__init__(core, None)
        self.jobs = []

    def submit(self, run, operation):
        if run["run_id"] not in self.pending:
            self.pending.add(run["run_id"])
            self.jobs.append((run["run_id"], operation))

    def finish(self, state, **values):
        rid, operation = self.jobs.pop(0)
        self.completed(rid, operation, dict(state=state, **values))


class DockerPolicyTests(unittest.TestCase):
    setUp = PolicyTests.setUp
    tearDown = PolicyTests.tearDown
    command = PolicyTests.command

    def start(self):
        spec = self.core.units["worker"]
        spec["preparation"] = dict(argv=["/bin/true"], timeout_seconds=1)
        spec["docker"] = dict(image="sha256:" + "a" * 64, argv=["/bin/sleep", "100"],
                              environment={}, mounts=[], gpu=False, prepared_input="effective")
        config = self.path / "input.json"
        config.write_bytes(b"{}")
        candidate = dict(schema_version=1, unit_id="worker", config_revision=self.revision, mode=None,
                         inputs=[dict(name="effective", path=str(config), sha256=hashlib.sha256(b"{}").hexdigest())])
        self.docker = self.core.docker = QueuedDocker(self.core)
        operation = self.core.handle(self.command(prepared=candidate))
        return self.core.runs[operation["run_id"]]

    def running(self):
        run = self.start()
        self.docker.finish("stopped", id="b" * 64)
        self.assertEqual(self.store.runs()[0]["docker"]["id"], "b" * 64)
        self.assertEqual(self.docker.jobs[0][1], "start")
        self.docker.finish("live", id="b" * 64)
        self.core.audit(full=True)
        return run

    def test_stop_waits_for_native_and_container_absence(self):
        run = self.running()
        op = self.core.handle(self.command("stop", expected_run=run["run_id"]))
        self.supervisor.exit(run)
        self.core.audit(full=True)
        self.assertTrue(run["blocked"])
        self.assertEqual(self.docker.jobs[0][1], "signal")
        self.docker.finish("stopped", id="b" * 64)
        self.core.audit(full=True)
        self.assertEqual(self.docker.jobs[0][1], "remove")
        self.docker.finish("absent")
        self.core.audit(full=True)
        self.assertFalse(run["blocked"])
        self.assertEqual(self.core.operations[op["request_id"]]["phase"], "complete")

    def test_stop_during_create_never_starts_container(self):
        run = self.start()
        self.core.handle(self.command("stop", expected_run=run["run_id"]))
        self.docker.finish("stopped", id="b" * 64)
        self.core.audit(full=True)
        self.assertNotIn("start", [job[1] for job in self.docker.jobs])
        self.assertEqual(self.supervisor.signals, 1)
        self.assertEqual(self.docker.jobs[0][1], "remove")

    def test_lost_create_reply_and_absence_remain_blocked_until_new_boot(self):
        run = self.start()
        self.docker.finish("unknown")
        self.supervisor.exit(run)
        self.clock.now += 1
        self.core.audit(full=True)
        self.docker.finish("absent")
        self.core.audit(full=True)
        self.assertTrue(run["blocked"])
        self.assertEqual(self.docker.state(run), "unknown")
        self.supervisor.services.clear()
        self.core.boot = "boot-two"
        self.core.audit(full=True)
        self.assertFalse(run["blocked"])

    def test_daemon_outage_and_ownership_conflict_retain_lock(self):
        for state in ("unknown", "conflict"):
            with self.subTest(state=state):
                run = self.running()
                self.supervisor.exit(run)
                self.clock.now += 1
                self.core.audit(full=True)
                self.docker.finish(state)
                self.core.audit(full=True)
                self.assertTrue(run["blocked"])
                self.assertIn(run["lifecycle"], ("outcome_unknown", "ownership_conflict"))
                # Separate durable run state for the next subcase.
                self.tearDown()
                self.setUp()

    def test_dead_launcher_requests_container_cleanup(self):
        run = self.running()
        self.supervisor.exit(run, 2)
        self.core.audit(full=True)
        self.assertEqual(self.docker.jobs[0][1], "signal")
        self.assertTrue(run["blocked"])

    def test_stubborn_container_times_out_without_kill_or_replacement(self):
        run = self.running()
        op = self.core.handle(self.command("stop", expected_run=run["run_id"]))
        self.docker.finish("live", id="b" * 64)
        self.supervisor.exit(run)
        self.clock.now += 3
        self.core.audit(full=True)
        self.assertEqual(self.core.operations[op["request_id"]]["phase"], "timed_out")
        self.assertTrue(run["blocked"])
        self.assertNotIn("signal", [job[1] for job in self.docker.jobs])

    def test_recovered_partial_start_never_replays_creation(self):
        run = self.start()
        recovered = Core(self.config, self.revision, self.store, "boot-one", clock=self.clock, wall=self.clock.wall)
        recovered.docker = QueuedDocker(recovered)
        recovered.connected(self.supervisor)
        self.assertEqual(recovered.docker.jobs[0][1], "inspect")
        self.assertEqual(recovered.runs[run["run_id"]]["docker"]["phase"], "unknown")

    def test_resolve_cannot_release_an_absent_service_with_unknown_container(self):
        run = self.start()
        self.supervisor.services.clear()
        self.docker.finish("unknown")
        with self.assertRaises(Fault) as exc:
            self.core.handle(self.command("resolve", expected_run=run["run_id"]))
        self.assertEqual(exc.exception.code, "precondition")
        self.assertTrue(run["blocked"])


class DockerProfileTests(unittest.TestCase):
    def test_mode_bound_is_32(self):
        value = profile()
        unit = value["units"][0]
        unit["launch_modes"] = [dict(id="mode" + str(i), label="Mode", argv=unit["argv"]) for i in range(32)]
        validate(value)
        unit["launch_modes"].append(dict(id="extra", label="Extra", argv=unit["argv"]))
        with self.assertRaises(Fault):
            validate(value)

    def test_rejects_mutable_image_and_socket_mounts(self):
        from robot_runtime.docker import validate as validate_docker
        spec = dict(image="sha256:" + "a" * 64, argv=["/bin/true"], environment={}, mounts=[], gpu=True, prepared_input="effective")
        validate_docker(spec)
        for replacement in (dict(image="repo:latest"), dict(mounts=[dict(source="/var/run/docker.sock", target="/socket")])):
            with self.assertRaises(Fault):
                validate_docker(dict(spec, **replacement))
