"""Declared independent choices, preflight, and persisted resolved identity."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import unittest
import test_core
from robot_runtime.common import Fault
from robot_runtime.profile import validate


def selected_profile():
    value = test_core.profile()
    unit = value["units"][0]
    unit["launch_selections"] = [dict(id=name, label=name.title(), choices=[dict(id=v, label=v) for v in choices])
                                 for name, choices in (("camera", ("a", "b")), ("model", ("cones", "objects")))]
    unit["launch_modes"] = [dict(id=camera + "-" + model, label=camera + " " + model,
                                argv=["/usr/bin/python3", "-c", "pass", camera, model],
                                selections=dict(camera=camera, model=model))
                            for camera in ("a", "b") for model in ("cones", "objects")]
    unit["default_launch_mode"] = "a-cones"
    unit["argv"] = unit["launch_modes"][0]["argv"]
    return value


class SelectionSchemaTests(unittest.TestCase):
    def test_rejects_ambiguous_missing_unknown_and_implicit_default(self):
        for mutation in (
            lambda u: u["launch_modes"][1].update(selections=dict(camera="a", model="cones")),
            lambda u: u["launch_modes"][1]["selections"].pop("camera"),
            lambda u: u["launch_modes"][1]["selections"].update(camera="unknown"),
            lambda u: u.pop("default_launch_mode"),
            lambda u: u.update(argv=["/bin/true"]),
            lambda u: u["launch_selections"][0]["choices"].append(dict(id="a", label="Duplicate")),
        ):
            value = selected_profile(); mutation(value["units"][0])
            with self.assertRaises(Fault): validate(value)


# Reuse the established policy fixture without repeating its inherited tests.
class SelectionPolicyTests(unittest.TestCase):
    setUp = test_core.PolicyTests.setUp
    tearDown = test_core.PolicyTests.tearDown
    command = test_core.PolicyTests.command
    restart = test_core.PolicyTests.restart
    assertFault = test_core.PolicyTests.assertFault

    def configure(self):
        self.config, self.revision = validate(selected_profile())
        self.restart()

    def test_all_four_fixed_launches_and_default_identity(self):
        self.configure()
        for mode in self.config["units"][0]["launch_modes"]:
            operation = self.core.handle(self.command(mode=mode["id"]))
            run = self.core.runs[operation["run_id"]]
            self.assertEqual(run["spec"]["argv"], mode["argv"])
            self.assertEqual(run["launch_mode"]["selections"], mode["selections"])
            self.restart()
            self.assertEqual(self.core.status()["units"][0]["launch_mode"]["selections"], mode["selections"])
            self.supervisor.exit(run); self.core.audit()
        default = self.core.handle(self.command())
        self.assertEqual(self.core.runs[default["run_id"]]["launch_mode"]["id"], "a-cones")
        self.assertNotIn("argv", self.core.status()["units"][0]["launch_modes"][0])

    def test_preflight_is_read_only_checks_paths_revision_and_selection(self):
        self.configure()
        running = self.core.handle(self.command())
        request = dict(version=1, action="prepare_launch", unit_id="worker", config_revision=self.revision, mode="b-objects")
        value = self.core.handle(request)
        self.assertEqual(value["launch_mode"]["selections"], dict(camera="b", model="objects"))
        self.assertEqual(value["status"]["units"][0]["run_id"], running["run_id"])
        self.assertEqual((self.supervisor.starts, self.supervisor.signals), (1, 0))
        self.assertFault("stale_config", lambda: self.core.handle(dict(request, config_revision="old")))
        self.assertFault("invalid_request", lambda: self.core.handle(dict(request, mode="unknown")))
        self.core.units["worker"]["launch_modes"][-1]["argv"] = ["/missing-fixture-executable"]
        self.assertFault("invalid_profile", lambda: self.core.handle(request))
