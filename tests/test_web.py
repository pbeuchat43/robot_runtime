"""Authorization boundaries and session behavior, without managed workloads."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import asyncio
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

from robot_runtime.auth import Auth, COOKIE, write_credential
from robot_runtime.common import Fault
from robot_runtime.web import application


class AuthTests(unittest.TestCase):
    def test_hash_sessions_expiry_rotation_and_rate_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "credential.json"
            write_credential(path, "example long test passphrase")
            self.assertNotIn("example long", path.read_text())
            now = [100.0]
            auth = Auth(path, clock=lambda: now[0])
            self.assertTrue(auth.verify("example long test passphrase"))
            self.assertFalse(auth.verify("wrong"))
            token, first = auth.issue()
            other, second = auth.issue()
            self.assertNotEqual(first["id"], second["id"])
            auth.revoke(token)
            self.assertIsNone(auth.session(token))
            self.assertIsNotNone(auth.session(other))
            now[0] += 1801
            self.assertIsNone(auth.session(other))
            token, _session = auth.issue()
            replacement = path.with_suffix(".replacement")
            write_credential(replacement, "another long test passphrase")
            os.replace(replacement, path)
            auth.reload()
            self.assertIsNone(auth.session(token))
            for _ in range(5):
                auth.allow_login()
            with self.assertRaises(Fault):
                auth.allow_login()


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        credential = self.root / "credential.json"
        write_credential(credential, "example long test passphrase")
        self.app = application(self.root, "https://robot.test:8443", credential)
        self.adapter = self.app["adapter"]
        self.calls = []
        async def rpc(action, **values):
            self.calls.append((action, values))
            if action == "log_target":
                return dict(public_logs=False)
            return dict(phase="complete")
        self.adapter.rpc = rpc
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.headers = {"Host": "robot.test:8443", "Origin": "https://robot.test:8443"}

    async def asyncTearDown(self):
        await self.client.close()
        self.temporary.cleanup()

    async def test_public_view_has_no_command_authority(self):
        result = await self.client.get("/api/status", headers=self.headers)
        self.assertEqual(result.status, 200)
        result = await self.client.post("/api/units/worker/start", json={}, headers=self.headers)
        self.assertEqual(result.status, 401)
        self.assertFalse(self.calls)

    async def test_every_script_referenced_by_the_page_is_served(self):
        page = await self.client.get("/", headers=self.headers)
        for path in re.findall(r'<script[^>]+src="([^"]+)"', await page.text()):
            response = await self.client.get(path, headers=self.headers)
            self.assertEqual(response.status, 200, path)
            self.assertEqual(response.content_type, "application/javascript", path)

    async def test_browser_client_modules_are_served_without_changing_the_page(self):
        page = await self.client.get("/", headers=self.headers)
        html = await page.text()
        for name in ("core", "http", "sse", "stream"):
            path = f"/assets/client-{name}.mjs"
            response = await self.client.get(path, headers=self.headers)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.content_type, "application/javascript")
            self.assertIn("export ", await response.text())
            self.assertNotIn(path, html)
        response = await self.client.get("/assets/client-private.mjs", headers=self.headers)
        self.assertEqual(response.status, 404)

    async def test_resource_cached_view_does_not_activate_monitoring(self):
        response = await self.client.get("/api/resources?history=true", headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.calls[-1], ("resource_status", dict(history=True)))
        self.assertIsNone(self.adapter.resources.task)
        response = await self.client.get("/api/resources?history=yes", headers=self.headers)
        self.assertEqual(response.status, 400)

    async def test_host_origin_and_csrf_fail_before_dispatch(self):
        token, session = self.adapter.auth.issue()
        valid = dict(self.headers, Cookie=COOKIE + "=" + token)
        for headers in (dict(valid, Host="attacker.test"), dict(valid, Origin="https://attacker.test"), valid):
            result = await self.client.post("/api/units/worker/start", json={}, headers=headers)
            self.assertEqual(result.status, 403)
        self.assertFalse(self.calls)
        valid["X-CSRF-Token"] = session["csrf"]
        result = await self.client.post("/api/units/worker/start", json=dict(request_id="request", expires_at_ms=123, config_revision="rev", mode="images"), headers=valid)
        self.assertEqual(result.status, 202)
        self.assertEqual(self.calls[-1][1]["actor"], "browser:" + session["id"])
        self.assertEqual(self.calls[-1][1]["mode"], "images")

    async def test_login_cookie_logout_revocation_and_private_logs(self):
        response = await self.client.post("/api/login", json=dict(passphrase="example long test passphrase"), headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertTrue(response.cookies[COOKIE]["secure"])
        self.assertTrue(response.cookies[COOKIE]["httponly"])
        self.assertEqual(response.cookies[COOKIE]["samesite"], "Strict")
        result = await response.json()
        headers = dict(self.headers, Cookie=COOKIE + "=" + response.cookies[COOKIE].value)
        response = await self.client.get("/api/runs/abc/logs", headers=self.headers)
        self.assertEqual(response.status, 401)
        headers["X-CSRF-Token"] = result["csrf"]
        response = await self.client.post("/api/logout", json={}, headers=headers)
        self.assertEqual(response.status, 200)
        response = await self.client.post("/api/units/worker/start", json={}, headers=headers)
        self.assertEqual(response.status, 401)

    async def test_no_profile_shell_or_local_resolution_route(self):
        token, session = self.adapter.auth.issue()
        headers = dict(self.headers, Cookie=COOKIE + "=" + token, **{"X-CSRF-Token": session["csrf"]})
        response = await self.client.post("/api/units/worker/resolve", json={}, headers=headers)
        self.assertEqual(response.status, 404)
        response = await self.client.post("/api/units/worker/start", json=dict(request_id="x", expires_at_ms=123, config_revision="rev", argv=["/bin/sh"]), headers=headers)
        self.assertEqual(response.status, 400)
        self.assertFalse(self.calls)

    async def test_observation_stream_closes_on_application_shutdown(self):
        response = await self.client.get("/api/events", headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.content.readline()).startswith(b"data: "))
        await self.app.shutdown()
        import asyncio
        await asyncio.wait_for(response.read(), 1)
        self.assertFalse(self.adapter.viewers)

    async def test_ros_commands_use_auth_csrf_and_capability_only(self):
        response = await self.client.post("/api/ros/services/enable", json={}, headers=self.headers)
        self.assertEqual(response.status, 401)
        token, session = self.adapter.auth.issue()
        headers = dict(self.headers, Cookie=COOKIE+"="+token)
        response = await self.client.post("/api/ros/services/enable", json={}, headers=headers)
        self.assertEqual(response.status, 403)
        headers["X-CSRF-Token"] = session["csrf"]
        value = dict(request_id="request", expires_at_ms=123, config_revision="rev", expected_run="run", generation="worker", arguments=dict(data=True))
        response = await self.client.post("/api/ros/services/enable", json=dict(value, service_name="/arbitrary"), headers=headers)
        self.assertEqual(response.status, 400)
        self.assertFalse(self.calls)
        response = await self.client.post("/api/ros/services/enable", json=value, headers=headers)
        self.assertEqual(response.status, 202)
        self.assertEqual(self.calls[-1][0], "ros_service")
        self.assertEqual(self.calls[-1][1]["capability_id"], "enable")
        self.assertEqual(self.calls[-1][1]["actor"], "browser:"+session["id"])

    async def test_topic_command_uses_fixed_choice_authentication_and_private_result(self):
        path = "/api/ros/commands/state"
        token, session = self.adapter.auth.issue()
        headers = dict(self.headers, Cookie=COOKIE+"="+token)
        for request_headers, expected in ((self.headers, 401), (headers, 403)):
            response = await self.client.post(path, json={}, headers=request_headers)
            self.assertEqual(response.status, expected)
        headers["X-CSRF-Token"] = session["csrf"]
        value = dict(request_id="request", expires_at_ms=123, config_revision="rev", expected_run="run", generation="worker", choice="disable")
        response = await self.client.post(path, json=dict(value, arguments=dict(data=2)), headers=headers)
        self.assertEqual(response.status, 400)
        self.assertFalse(self.calls)
        response = await self.client.post(path, json=value, headers=headers)
        self.assertEqual(response.status, 202)
        self.assertEqual(self.calls[-1][0], "ros_topic_command")
        self.assertEqual(self.calls[-1][1]["choice"], "disable")
        self.adapter.rpc = AsyncMock(return_value=dict(action="ros_topic_command", phase="complete"))
        response = await self.client.get("/api/operations/request", headers=self.headers)
        self.assertEqual(response.status, 401)

    async def test_ros_stream_paces_selected_topics_and_coalesces_latest_state(self):
        self.adapter.rpc = AsyncMock(return_value=dict(topics=[
            dict(id="slow", max_rate_hz=5), dict(id="unselected", max_rate_hz=20)]))
        self.adapter.ros.sync = AsyncMock()
        response = await self.client.get("/api/ros/events?topic=slow", headers=self.headers)
        sequence = 0
        async def publish():
            nonlocal sequence
            while True:
                sequence += 1
                self.adapter.ros.publish(dict(topics=[], sequence=sequence))
                await asyncio.sleep(.005)
        producer = asyncio.create_task(publish())
        try:
            frames = []
            while len(frames) < 5:
                line = await asyncio.wait_for(response.content.readline(), 2)
                if line.startswith(b"data: "):
                    frames.append((asyncio.get_running_loop().time(), json.loads(line[6:]), sequence))
            self.assertGreaterEqual(frames[-1][0] - frames[0][0], .75)
            self.assertGreater(frames[-1][1]["sequence"], 20)
            for _time, frame, current in frames[1:]:
                self.assertLessEqual(current - frame["sequence"], 3)
            await self.adapter.ros.close()
            await asyncio.wait_for(response.read(), 1)
            self.assertFalse(self.adapter.ros.viewers)
        finally:
            producer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await producer

    async def test_multiplex_selection_baseline_updates_and_cleanup(self):
        self.adapter.rpc = AsyncMock(return_value=dict(topics=[dict(id="counter", max_rate_hz=5)]))
        self.adapter.ros.sync = AsyncMock()
        for query in ("", "?channel=bogus", "?channel=runtime&topic=counter",
                      "?channel=observations", "?channel=runtime&channel=runtime"):
            reply = await self.client.get("/api/stream" + query, headers=self.headers)
            self.assertEqual(reply.status, 400, query)
        reply = await self.client.get("/api/stream?channel=runtime&channel=observations&topic=missing", headers=self.headers)
        self.assertEqual(reply.status, 404)
        response = await self.client.get("/api/stream?channel=runtime&channel=observations&topic=counter", headers=self.headers)
        async def frame():
            while True:
                line = await asyncio.wait_for(response.content.readline(), 2)
                if line.startswith(b"data: "):
                    return json.loads(line[6:])
        initial = await frame()
        self.assertEqual(initial["version"], 2)
        self.assertEqual(initial["sequence"], 1)
        self.assertIn("runtime", initial)
        self.assertTrue(initial["observations"]["reset"])
        self.adapter.ros.publish(dict(available=True, topics=[dict(id="counter", stale_after_seconds=1,
            sequence=1, fresh=True, age_seconds=0, status="current", data=dict(data="1"))]))
        value = await frame()
        self.assertNotIn("runtime", value)
        self.assertEqual(value["sequence"], 2)
        self.assertEqual(value["stream_id"], initial["stream_id"])
        self.assertEqual(value["observations"]["updates"][0]["value"]["data"], dict(data="1"))
        self.adapter.closing = True
        for channel in self.adapter.stream_viewers:
            channel.put_nowait(False)
        await asyncio.wait_for(response.read(), 1)
        self.assertFalse(self.adapter.stream_viewers)
        self.assertFalse(self.adapter.viewers)
        self.assertFalse(self.adapter.ros.viewers)

    async def test_action_commands_require_auth_and_expose_only_declared_targets(self):
        original_rpc = self.adapter.rpc
        response = await self.client.get("/api/ros/actions", headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.calls[-1][0], "ros_actions")
        token, session = self.adapter.auth.issue()
        headers = dict(self.headers, Cookie=COOKIE+"="+token)
        for suffix, action, payload in (("goals", "ros_action_goal", dict(arguments=dict(steps=3))),
                                        ("cancel", "ros_action_cancel", dict(goal_request_id="goal"))):
            path = "/api/ros/actions/count/" + suffix
            for request_headers, expected in ((self.headers, 401), (headers, 403)):
                response = await self.client.post(path, json={}, headers=request_headers)
                self.assertEqual(response.status, expected)
            authorized = dict(headers, **{"X-CSRF-Token": session["csrf"]})
            value = dict(request_id="request", expires_at_ms=123, config_revision="rev", expected_run="run", generation="worker", **payload)
            response = await self.client.post(path, json=dict(value, action_name="/arbitrary"), headers=authorized)
            self.assertEqual(response.status, 400)
            response = await self.client.post(path, json=value, headers=authorized)
            self.assertEqual(response.status, 202)
            self.assertEqual(self.calls[-1][0], action)
            self.assertEqual(self.calls[-1][1]["actor"], "browser:"+session["id"])
            self.adapter.rpc = AsyncMock(return_value=dict(action=action, phase="complete"))
            response = await self.client.get("/api/operations/request", headers=self.headers)
            self.assertEqual(response.status, 401)
            response = await self.client.get("/api/operations/request", headers=authorized)
            self.assertEqual(response.status, 200)
            self.adapter.rpc = original_rpc

    async def test_ros_results_require_authentication_and_no_recovery_route_exists(self):
        async def rpc(action, **values):
            return dict(action="ros_service", phase="complete", response=dict(message="private result"))
        self.adapter.rpc = rpc
        response = await self.client.get("/api/operations/request", headers=self.headers)
        self.assertEqual(response.status, 401)
        self.assertNotIn("private result", await response.text())
        response = await self.client.post("/api/ros/acknowledge", json={}, headers=self.headers)
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
