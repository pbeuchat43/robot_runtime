"""Prepared-input and coordinated-restart regressions on the Linux test host."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from robot_runtime import prepared
from robot_runtime.common import Fault
from robot_runtime.profile import validate
from robot_runtime.supervisor import launch_argv
import test_core


class RestartTests(unittest.TestCase):
    setUp = test_core.PolicyTests.setUp
    tearDown = test_core.PolicyTests.tearDown
    command = test_core.PolicyTests.command
    start = test_core.PolicyTests.start
    reload_manager = test_core.PolicyTests.restart
    assertFault = test_core.PolicyTests.assertFault

    def candidate(self, content=b'{"threshold":0.6}'):
        source = self.path / "request.json"
        source.write_bytes(content)
        return dict(schema_version=1, unit_id="worker", config_revision=self.revision, mode=None,
                    inputs=[dict(name="configuration", path=str(source), sha256=hashlib.sha256(content).hexdigest())])

    def begin(self, run, candidate=None, **extra):
        values = dict(expected_run=run["run_id"], **extra)
        if candidate is not None:
            values["prepared"] = candidate
        return self.core.handle(self.command("restart", **values))

    def finish_stop(self, run):
        self.supervisor.exit(run)
        self.core.audit()
        self.core.audit()

    def test_bad_candidate_leaves_old_run_running(self):
        old = self.start()
        candidate = self.candidate()
        candidate["inputs"][0]["sha256"] = "0" * 64
        self.assertFault("invalid_prepared", lambda: self.begin(old, candidate))
        self.assertEqual((self.supervisor.starts, self.supervisor.signals), (1, 0))
        self.assertTrue(old["blocked"])

    def test_snapshot_survives_source_edits_and_is_not_public(self):
        old = self.start()
        candidate = self.candidate()
        operation = self.begin(old, candidate)
        self.assertNotIn("replacement", operation)
        self.assertNotIn(str(self.path), json.dumps(operation))
        (self.path / "request.json").write_text('{"threshold":0.7}')
        self.finish_stop(old)
        result = self.core.operations[operation["request_id"]]
        self.assertEqual(result["phase"], "complete")
        new = self.core.current("worker")
        self.assertNotEqual(new["run_id"], old["run_id"])
        self.assertEqual(prepared.load(new["prepared"])[1]["configuration"], b'{"threshold":0.6}')
        self.assertIn("ROBOT_RUNTIME_PREPARED=" + new["prepared"]["path"], launch_argv(new))
        self.assertEqual((result["old_run_id"], result["new_run_id"]), (old["run_id"], new["run_id"]))

    def test_wrong_unit_or_revision_fails_before_stop(self):
        old = self.start()
        for key, value in (("unit_id", "another"), ("config_revision", "old"), ("mode", "another")):
            self.assertFault("stale_prepared", lambda: self.begin(old, dict(self.candidate(), **{key: value})))
        self.assertEqual(self.supervisor.signals, 0)

    def test_restart_reserves_transition_and_duplicate_does_not_signal_twice(self):
        old = self.start()
        request = self.command("restart", expected_run=old["run_id"])
        result = self.core.handle(request)
        self.assertEqual(result, self.core.handle(request))
        self.assertFault("busy", lambda: self.core.handle(self.command("stop", expected_run=old["run_id"])))
        self.assertFault("busy", lambda: self.core.handle(self.command()))
        self.assertEqual(self.supervisor.signals, 1)
        self.finish_stop(old)
        self.assertEqual(self.supervisor.starts, 2)

    def test_stop_deadline_never_starts_late_replacement(self):
        old = self.start()
        operation = self.begin(old)
        self.clock.now += 3
        self.finish_stop(old)
        self.assertEqual(self.core.operations[operation["request_id"]]["phase"], "timed_out")
        self.assertEqual(self.supervisor.starts, 1)

    def test_manager_recovery_during_stop_does_not_continue_dispatch(self):
        old = self.start()
        operation = self.begin(old)
        self.reload_manager()
        self.finish_stop(self.core.runs[old["run_id"]])
        result = self.core.operations[operation["request_id"]]
        self.assertEqual((result["phase"], result["outcome"]), ("failed", "stopped_without_replacement"))
        self.assertEqual(self.supervisor.starts, 1)

    def test_lost_stop_reply_does_not_authorize_replacement(self):
        old = self.start()
        with patch.object(self.supervisor, "signal", side_effect=OSError("reply lost")):
            operation = self.begin(old)
        self.finish_stop(old)
        self.assertEqual(self.core.operations[operation["request_id"]]["outcome"], "stopped_without_replacement")
        self.assertEqual(self.supervisor.starts, 1)

    def test_lost_replacement_reply_is_reconciled_without_replay(self):
        old = self.start()
        operation = self.begin(old)
        self.supervisor.crash = "after"
        self.finish_stop(old)
        self.reload_manager()
        self.assertEqual(self.core.operations[operation["request_id"]]["phase"], "complete")
        self.assertEqual(self.supervisor.starts, 2)

    def test_missing_replacement_can_be_explicitly_resolved(self):
        old = self.start()
        operation = self.begin(old)
        self.supervisor.crash = "before"
        self.finish_stop(old)
        missing = self.core.current("worker")
        self.assertEqual(self.core.operations[operation["request_id"]]["phase"], "outcome_unknown")
        self.core.handle(self.command("resolve", expected_run=missing["run_id"]))
        self.assertEqual(self.core.operations[operation["request_id"]]["phase"], "failed")
        self.supervisor.crash = None
        self.start()
        self.assertEqual(self.supervisor.starts, 2)

    def test_corrupted_frozen_input_prevents_replacement(self):
        old = self.start()
        operation = self.begin(old, self.candidate())
        reference = self.core.operations[operation["request_id"]]["replacement"]["prepared"]
        path = Path(reference["path"]).with_name("configuration")
        path.chmod(0o600)
        path.write_text("changed")
        self.finish_stop(old)
        self.assertEqual(self.core.operations[operation["request_id"]]["outcome"], "replacement_inputs_unavailable")
        self.assertEqual(self.supervisor.starts, 1)

    def test_failed_replacement_is_reported_as_failure(self):
        old = self.start()
        operation = self.begin(old)
        start = self.supervisor.start
        def failing(run):
            start(run)
            self.supervisor.exit(run, status=7)
        with patch.object(self.supervisor, "start", side_effect=failing):
            self.finish_stop(old)
        result = self.core.operations[operation["request_id"]]
        self.assertEqual((result["phase"], result["outcome"]), ("failed", "replacement_failed"))

    def test_restart_retains_the_current_selected_mode(self):
        source = test_core.profile()
        source["units"][0]["launch_modes"] = [dict(id="alternate", label="Alternate", argv=["/usr/bin/python3", "-c", "print(1)"])]
        self.config, self.revision = validate(source)
        self.reload_manager()
        result = self.core.handle(self.command(mode="alternate"))
        old = self.core.runs[result["run_id"]]
        self.begin(old)
        self.finish_stop(old)
        self.assertEqual(self.core.current("worker")["launch_mode"]["id"], "alternate")


class PreparedCommandTests(unittest.TestCase):
    def test_named_preparer_receives_only_declared_environment_and_context(self):
        script = 'import json,os;print(json.dumps({k:v for k,v in os.environ.items() if k.startswith("ROBOT_RUNTIME_")}))'
        result = prepared.execute(dict(unit_id="worker", config_revision="revision", mode="selected",
            preparation=dict(argv=["/usr/bin/python3", "-c", script], timeout_seconds=2),
            environment={}, working_directory="/tmp"))
        self.assertEqual(result, dict(ROBOT_RUNTIME_PREPARE_UNIT="worker", ROBOT_RUNTIME_PREPARE_REVISION="revision", ROBOT_RUNTIME_PREPARE_MODE="selected"))

    def test_oversized_or_nonregular_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large"
            path.write_bytes(b"x" * (prepared.MAX_INPUT + 1))
            with self.assertRaises(Fault):
                prepared.read_file(path)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(Fault):
                prepared.read_file(link)

    def test_failed_preparer_has_specific_failure(self):
        with self.assertRaises(Fault) as caught:
            prepared.execute(dict(unit_id="worker", config_revision="revision", mode=None,
                preparation=dict(argv=["/usr/bin/python3", "-c", "raise SystemExit(1)"], timeout_seconds=2),
                environment={}, working_directory="/tmp"))
        self.assertEqual(caught.exception.code, "preparation_failed")
