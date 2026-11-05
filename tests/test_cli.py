"""Shared CLI policy and side-effect boundaries; execute on the Linux host."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from robot_runtime import cli
from robot_runtime.common import Fault


class CliTests(unittest.TestCase):
    def status(self):
        return dict(config_revision="revision", units=[dict(unit_id="worker", run_id="a" * 32)])

    def invoke(self, argv, response):
        output = io.StringIO()
        with patch.object(cli, "request", side_effect=response) as request, contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            cli.main(argv)
        return json.loads(output.getvalue()), request.call_args_list

    def test_aliases_use_identical_arguments_and_requests(self):
        results = []
        for spelling in ("log", "logs"):
            results.append(self.invoke([spelling, "worker", "--limit", "7"],
                [self.status(), dict(records=[], next_cursor=None)]))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0]["run_id"], "a" * 32)

    def test_explicit_historical_run_needs_no_current_status(self):
        result, calls = self.invoke(["log", "--run", "b" * 32], [dict(records=[], next_cursor=None)])
        self.assertEqual(result["run_id"], "b" * 32)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[1], "logs")

    def test_follow_never_retargets_to_a_replacement(self):
        args = cli.build_parser().parse_args(["log", "worker", "--follow"])
        with patch.object(cli, "request", side_effect=[self.status(),
                dict(records=[{"message": "one"}], next_cursor="first"),
                dict(records=[{"message": "two"}], next_cursor="second")]) as request, patch.object(cli.time, "sleep"):
            pages = cli.log_pages(args)
            self.assertEqual(next(pages)["run_id"], "a" * 32)
            self.assertEqual(next(pages)["run_id"], "a" * 32)
            self.assertEqual(request.call_count, 3)
            self.assertEqual(request.call_args.kwargs["cursor"], "first")
            pages.close()

    def test_stop_discovers_once_and_submits_exact_identity(self):
        result, calls = self.invoke(["stop", "worker"], [self.status(), {"phase": "in_progress"}])
        self.assertEqual(result["phase"], "in_progress")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1].kwargs["expected_run"], "a" * 32)
        self.assertEqual(calls[-1].kwargs["config_revision"], "revision")

    def test_stale_stop_is_not_retried_against_another_run(self):
        with patch.object(cli, "request", side_effect=[self.status(), Fault("stale_run", "replaced")]) as request, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["stop", "worker"])
        self.assertEqual(request.call_count, 2)

    def test_catalog_validates_metadata_without_installed_paths_or_ipc(self):
        profile = dict(schema_version=1, robot_id="example", namespace="example", units=[dict(
            id="worker", label="Example worker", argv=["/absent/example"],
            working_directory="/absent", environment={}, stop=dict(signal="SIGINT", timeout_seconds=2))])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps(profile))
            result, calls = self.invoke(["catalog", "--profile", str(path)], [])
        self.assertEqual(calls, [])
        self.assertEqual(result["units"][0]["unit_id"], "worker")

    def test_embedded_parser_keeps_parent_command_and_help_is_inert(self):
        import argparse
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command")
        cli.configure_parser(commands.add_parser("runtime"))
        args = parser.parse_args(["runtime", "log", "worker"])
        self.assertEqual((args.command, args.runtime_command), ("runtime", "log"))
        with patch.object(cli, "request") as request, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                cli.main(["log", "--help"])
        self.assertEqual(result.exception.code, 0)
        request.assert_not_called()
