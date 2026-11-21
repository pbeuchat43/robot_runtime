"""CLI presentation contracts without changing domain payloads or dispatch policy."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from robot_runtime import cli
from robot_runtime.common import Fault
from robot_runtime.output import emit, emit_logs


class OutputTests(unittest.TestCase):
    def test_nested_records_and_empty_values_are_readable_and_lossless_in_json(self):
        value = dict(valid=True, units=[dict(unit_id="camera", available=False,
            modes=[dict(id="rgb", label="Colour")])], missing=None, empty=[],
            parameters=dict(enabled=False, values=[1, 2]), path=Path("/profile"))
        out = io.StringIO()
        with redirect_stdout(out):
            emit(value, structured=False)
        human = out.getvalue()
        for text in ("Valid: yes", "Unit id", "camera", "Colour", "Missing: none", "Empty: (none)", "Enabled: no", "/profile"):
            self.assertIn(text, human)
        self.assertNotIn('{', human)
        self.assertNotIn("'", human)
        out = io.StringIO()
        with redirect_stdout(out):
            emit(value, structured=True)
        self.assertEqual(json.loads(out.getvalue()), dict(value, path="/profile"))

    def test_terminal_controls_cannot_rewrite_human_rows(self):
        out = io.StringIO()
        with redirect_stdout(out):
            emit(dict(message="bad\x1b[2J\nnext"), structured=False)
        self.assertNotIn("\x1b", out.getvalue())
        self.assertIn(r"\nnext", out.getvalue())

    def test_status_does_not_repeat_catalog_but_preserves_current_diagnostics(self):
        value = dict(robot_id="robot", fresh=False, error="stale audit", conflicts=["foreign"],
            config_revision="rev", recording=dict(overlays_by_mode={"catalog-mode": ["cones"]}),
            units=[dict(unit_id="camera", lifecycle="ownership_conflict", ownership="unknown", fresh=False,
                blocked=True, run_id="run", launch_mode=dict(id="active-mode", label="Active"),
                launch_modes=[dict(id="catalog-mode")], launch_selections=[dict(id="model")],
                unavailable_reason="disconnected", observation_age_seconds=42,
                recording=dict(fresh=False, state="interrupted"))])
        original = json.dumps(value)
        with redirect_stdout(io.StringIO()) as out:
            emit(value, structured=False, stream=True)
        for text in ("ownership_conflict", "unknown", "active-mode", "Run id: run", "stale audit",
                     "foreign", "disconnected", "Observation age seconds: 42", "interrupted", "Config revision: rev"):
            self.assertIn(text, out.getvalue())
        self.assertNotIn("catalog-mode", out.getvalue())
        self.assertNotIn("Launch selections", out.getvalue())
        self.assertEqual(json.dumps(value), original)
        with redirect_stdout(io.StringIO()) as out:
            emit(value, structured=True)
        self.assertEqual(json.loads(out.getvalue()), value)

    def test_human_logs_preserve_identity_gaps_and_suppress_empty_polls(self):
        empty = dict(run_id="retained", records=[], next_cursor="cursor", gap=None,
                     truncated=False, history_complete=False)
        out = io.StringIO()
        with redirect_stdout(out):
            emit_logs(empty, structured=False, follow=True)
            initial = out.getvalue()
            emit_logs(empty, structured=False, follow=True, first=False)
            self.assertEqual(out.getvalue(), initial)
            emit_logs(dict(empty, gap="cursor_not_retained", truncated=True, records=[
                dict(realtime_us="1234567890123456", message="new message", message_truncated=True)]),
                structured=False, follow=True, first=False)
        for text in ("Run id: retained", "History complete: no", "cursor_not_retained", "Truncated: yes",
                     "2009-02-13T23:31:30.123456Z  new message [message truncated]"):
            self.assertIn(text, out.getvalue())

    def test_history_columns_are_named_and_watch_is_bounded(self):
        value = dict(history_columns=["time_ms", "cpu_percent"], history=[[123, 2.5], [124, None]])
        out = io.StringIO()
        with redirect_stdout(out):
            emit(value, structured=False)
        self.assertIn("Cpu percent", out.getvalue())
        self.assertIn("2.5", out.getvalue())
        self.assertIn("none", out.getvalue())
        with redirect_stdout(io.StringIO()) as out:
            emit(value, structured=False, stream=True)
        self.assertIn("2 samples", out.getvalue())
        self.assertNotIn("2.5", out.getvalue())
        with redirect_stdout(io.StringIO()) as out:
            emit(value, structured=True, stream=True)
        self.assertEqual(json.loads(out.getvalue()), value)


class RuntimeOutputTests(unittest.TestCase):
    def capture(self, argv, **mocks):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch.object(cli, "request", **mocks):
            result = cli.main(argv)
        return result, out.getvalue(), err.getvalue()

    def test_every_finite_ipc_family_preserves_payload_in_both_modes(self):
        # This response includes the metadata needed by guards/read polling; the
        # test verifies rendering does not select, drop or rename result fields.
        result = dict(config_revision="revision", generation="generation", server_time_ms=123,
            units=[dict(unit_id="worker", run_id="a" * 32)], reading=False, read_status="complete",
            request_id="lookup", phase="outcome_unknown", result=dict(accepted=False, reason="inspect first"))
        routes = [
            ["status", "--fresh"], ["resources", "--history"], ["operation", "lookup"], ["inspect", "a" * 32],
            ["start", "worker"], ["stop", "worker"], ["restart", "worker"],
            ["resolve", "worker", "--expected-run", "a" * 32],
            ["ros", "status"], ["ros", "actions"],
            ["ros", "command", "vehicle", "hold", "--expected-run", "a" * 32],
            ["ros", "call", "service", "--expected-run", "a" * 32],
            ["ros", "acknowledge", "lookup", "--expected-run", "a" * 32],
            ["ros", "action", "send", "goal", "--expected-run", "a" * 32],
            ["ros", "action", "cancel", "goal", "--goal", "lookup", "--expected-run", "a" * 32],
            ["ros", "parameters", "read", "camera"],
            ["ros", "parameters", "set", "camera", "--changes", "{}", "--expected-snapshot", "snap", "--expected-run", "a" * 32],
        ]
        for argv in routes:
            for flag in ([], ["--json"], ["--human"]):
                with self.subTest(argv=argv, flag=flag):
                    _, out, _ = self.capture([*argv, *flag], return_value=result)
                    if flag == ["--human"]:
                        self.assertIn("Phase: outcome_unknown", out)
                        self.assertIn("Accepted: no", out)
                        self.assertNotIn('{', out)
                    else:
                        self.assertEqual(json.loads(out), result)

    def test_local_profile_and_credential_commands_use_selected_format(self):
        catalog = dict(robot_id="robot", units=[dict(unit_id="worker", available=True)])
        for flag in ("--human", "--json"):
            with patch("robot_runtime.profile.catalog", return_value=catalog):
                _, out, _ = self.capture([flag, "catalog", "--profile", "/profile"])
            if flag == "--json":
                self.assertEqual(json.loads(out), catalog)
            else:
                self.assertIn("Robot id: robot", out)
            with patch("robot_runtime.profile.read", return_value=(dict(units=[dict(id="worker")]), "rev")):
                _, out, _ = self.capture(["validate", "/profile", flag])
            self.assertIn('"valid": true' if flag == "--json" else "Valid: yes", out)
            with patch("robot_runtime.auth.create_credential") as create:
                _, out, _ = self.capture(["credential", "--file", "/credential", flag])
            create.assert_called_once_with(Path("/credential"))
            self.assertIn('"created": true' if flag == "--json" else "Created: yes", out)

    def test_all_watch_sources_keep_json_lines_and_readable_updates(self):
        frames = [dict(fresh=True, values=[dict(name="x", value="1234567890123456789")]), dict(fresh=False, values=[])]
        for argv in (["watch"], ["resources", "--watch"], ["ros", "watch", "--topic", "pose"], ["ros", "actions", "--watch"]):
            for flag in ("--json", "--human"):
                with self.subTest(argv=argv, flag=flag), patch.object(cli, "watch", return_value=iter(frames)), patch.object(cli.time, "sleep"):
                    _, out, _ = self.capture([*argv, flag], side_effect=[*frames, KeyboardInterrupt()])
                if flag == "--json":
                    self.assertEqual([json.loads(line) for line in out.splitlines()], frames)
                else:
                    self.assertEqual(out.count("--- Update ---"), 2)
                    self.assertIn("Fresh: yes", out)
                    self.assertIn("Fresh: no", out)

    def test_logs_and_alias_follow_keep_empty_json_pages(self):
        pages = [dict(run_id="retained", records=[], next_cursor=None),
                 dict(run_id="retained", records=[dict(message="hello")], next_cursor="next")]
        for name in ("log", "logs"):
            for follow in ([], ["--follow"]):
                with patch.object(cli, "log_pages", return_value=iter(pages if follow else pages[:1])):
                    _, out, _ = self.capture([name, "worker", "--json", *follow])
                decoded = [json.loads(line) for line in out.splitlines()] if follow else [json.loads(out)]
                self.assertEqual(decoded, pages if follow else pages[:1])

    def test_errors_preserve_code_details_and_exit_status(self):
        for flag in ([], ["--json"], ["--human"]):
            for error in (Fault("stale_run", "run replaced", expected_run="old"), OSError("unavailable")):
                out, err = io.StringIO(), io.StringIO()
                with self.subTest(flag=flag, error=error), redirect_stdout(out), redirect_stderr(err), \
                     patch.object(cli, "request", side_effect=error), self.assertRaises(SystemExit) as exited:
                    cli.main([*flag, "status"])
                self.assertEqual(exited.exception.code, 1)
                self.assertEqual(out.getvalue(), "")
                if flag == ["--human"]:
                    self.assertIn("Error [", err.getvalue())
                    if isinstance(error, Fault):
                        self.assertIn("Expected run: old", err.getvalue())
                else:
                    expected = error.as_dict() if isinstance(error, Fault) else dict(code="io_error", message=str(error))
                    self.assertEqual(json.loads(err.getvalue()), dict(error=expected))

    def test_request_id_is_flushed_before_submission_even_on_lost_response(self):
        for flag in ("--json", "--human"):
            err = io.StringIO()
            def submit(*_args, **_kwargs):
                self.assertEqual(err.getvalue(), "Request ID: lookup\n")
                raise Fault("transport_unknown", "lost response")
            with redirect_stderr(err), patch.object(cli, "request", side_effect=submit), self.assertRaises(SystemExit) as exited:
                cli.main(["start", "worker", "--config-revision", "revision", "--request-id", "lookup", flag])
            self.assertEqual(exited.exception.code, 1)
            self.assertIn("transport_unknown", err.getvalue())

    def test_parser_flags_at_each_level_and_argument_errors(self):
        for argv in (["--json", "ros", "status"], ["ros", "--json", "status"], ["ros", "status", "--json"]):
            self.assertTrue(cli.build_parser().parse_args(argv).structured)
        for flag in ([], ["--human"], ["--json"]):
            err = io.StringIO()
            with redirect_stderr(err), self.assertRaises(SystemExit) as exited:
                cli.main([*flag, "ros", "parameters", "set"])
            self.assertEqual(exited.exception.code, 2)
            if flag == ["--json"]:
                self.assertEqual(json.loads(err.getvalue())["error"]["code"], "invalid_arguments")
            else:
                self.assertIn("usage:", err.getvalue())
