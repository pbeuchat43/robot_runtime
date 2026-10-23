"""Isolated HTTPS and real headless-Chromium workflow, executed on jah."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import time
import uuid
from pathlib import Path

import aiohttp

from robot_runtime.auth import write_credential
from robot_runtime.common import Fault
from robot_runtime.ipc import request


def port():
    with socket.socket() as descriptor:
        descriptor.bind(("127.0.0.1", 0))
        return descriptor.getsockname()[1]


async def wait_for(function, seconds=15):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        try:
            result = await function()
            if result:
                return result
        except (OSError, Fault, aiohttp.ClientError):
            pass
        await asyncio.sleep(0.1)
    raise AssertionError("browser condition timed out")


class CDP:
    def __init__(self, websocket):
        self.ws = websocket
        self.sequence = 0
        self.exceptions = []

    async def call(self, method, **params):
        self.sequence += 1
        identifier = self.sequence
        await self.ws.send_json(dict(id=identifier, method=method, params=params))
        while True:
            message = await self.ws.receive_json(timeout=15)
            if message.get("method") == "Runtime.exceptionThrown":
                details = message["params"]["exceptionDetails"]
                self.exceptions.append(details.get("exception", {}).get("description") or details.get("text", "JavaScript exception"))
            if message.get("id") == identifier:
                if "error" in message:
                    raise RuntimeError("DevTools operation failed: " + method)
                return message.get("result", {})

    async def evaluate(self, expression):
        result = await self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in result:
            raise RuntimeError("Browser expression failed")
        return result.get("result", {}).get("value")


async def main(root, *, ros=False, parameters=False, observations=False, snapshots=False, imu=False, lidar=False, fiducials=False, resources=False, actions=False, commands=False, foxglove=False, delivery=False, av_native=None):
    root = root.resolve()
    assert root.parent == Path("/tmp") and re.fullmatch(r"robot-runtime-(?:slice|ros)-[0-9a-f]{12}", root.name)
    token = uuid.uuid4().hex[:10]
    area = root / ("web-" + token)
    area.mkdir(mode=0o700)
    state = area / "state"
    profile = dict(schema_version=1, robot_id="test-robot", namespace="w" + token, units=[
        dict(id="idle", label="Software fixture", argv=[sys.executable, str(Path(__file__).with_name("workload.py").resolve()), "idle", str(area / "events.jsonl")],
             working_directory=str(area), environment={}, stop=dict(signal="SIGINT", timeout_seconds=2), public_logs=True)])
    if ros:
        from ros_fixture import make_profile
        profile = make_profile(area, token, include_limits=False)
    if parameters:
        from parameter_fixture import make_profile
        profile = make_profile(area, token)
    if observations:
        from observations_fixture import make_profile
        profile = make_profile(area, token)
    if snapshots:
        from snapshot_fixture import make_profile
        profile = make_profile(area, token)
    if lidar:
        from lidar_fixture import make_profile
        profile = make_profile(area, token)
    if imu:
        from imu_fixture import make_profile
        profile = make_profile(area, token)
    if fiducials:
        from fiducials_fixture import make_profile
        profile = make_profile(area, token)
    if actions:
        from action_fixture import make_profile
        profile = make_profile(area, token)
    if commands:
        from command_fixture import make_profile
        profile = make_profile(area, token)
    if foxglove:
        from foxglove_fixture import make_profile
        profile = make_profile(area, token)
    if av_native:
        from av_fixture import configure
        profile = configure(profile, area, av_native)
    (area / "profile.json").write_text(json.dumps(profile))
    passphrase = secrets.token_urlsafe(24)
    write_credential(area / "credential.json", passphrase)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(area / "key.pem"),
                    "-out", str(area / "cert.pem"), "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.chmod(area / "key.pem", 0o600)
    public_key = subprocess.check_output(["openssl", "x509", "-in", str(area / "cert.pem"), "-pubkey", "-noout"])
    spki = subprocess.check_output(["openssl", "pkey", "-pubin", "-outform", "der"], input=public_key)
    fingerprint = base64.b64encode(hashlib.sha256(spki).digest()).decode()
    https_port, debug_port = port(), port()
    origin = "https://localhost:" + str(https_port)
    context = ssl.create_default_context(cafile=str(area / "cert.pem"))
    manager_log = (area / "manager.log").open("w")
    web_log = (area / "web.log").open("w")
    manager = subprocess.Popen([sys.executable, "-m", "robot_runtime.manager", "--profile", str(area / "profile.json"), "--state", str(state)], stdout=manager_log, stderr=manager_log)
    web_process = None
    browser_name = "drt-ui-" + token + ".service"
    browser_started = False
    browser_groups = set()
    browser_profile = Path.home() / "snap/chromium/common" / ("robot-runtime-test-" + token)
    browser_profile.mkdir(mode=0o700)
    report = dict(token=token, checks=[], origin=origin, browser_profile=str(browser_profile),
                  tls="Temporary certificate trusted by the API client; Chromium trusts only its SPKI for this test")

    def launch_web():
        return subprocess.Popen([sys.executable, "-m", "robot_runtime.web", "--state", str(state), "--origin", origin,
                                 "--credential", str(area / "credential.json"), "--cert", str(area / "cert.pem"), "--key", str(area / "key.pem"),
                                 "--bind", "127.0.0.1", "--port", str(https_port)], stdout=web_log, stderr=web_log)

    def check(name, **values):
        report["checks"].append(dict(check=name, passed=True, **values))

    async def local(action, **values):
        return await asyncio.to_thread(request, state, action, **values)

    async def restart_manager():
        nonlocal manager
        manager.terminate()
        await asyncio.to_thread(manager.wait, timeout=10)
        manager = subprocess.Popen(manager.args, stdout=manager_log, stderr=manager_log)
        await wait_for(ready)

    async def ready():
        return (await local("status"))["fresh"]

    try:
        await wait_for(ready)
        web_process = launch_web()
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=context)) as client:
            async def api(path, method="GET", body=None, extra=None):
                headers = {"Origin": origin, **(extra or {})}
                async with client.request(method, origin + path, json=body, headers=headers) as response:
                    return response.status, await response.json(), dict(response.headers)

            async def web_ready():
                code, value, _headers = await api("/api/status")
                return code == 200 and value["fresh"]
            await wait_for(web_ready)
            code, _value, headers = await api("/api/status")
            assert code == 200 and "script-src 'self'" in headers["Content-Security-Policy"]
            code, _value, _headers = await api("/api/units/idle/start", "POST", {})
            assert code == 401
            check("public_status_cannot_command")
            code, session, headers = await api("/api/login", "POST", dict(passphrase=passphrase))
            assert code == 200 and "Secure" in headers["Set-Cookie"] and "HttpOnly" in headers["Set-Cookie"]
            command_headers = {"X-CSRF-Token": session["csrf"]}
            for extra in ({}, {**command_headers, "Origin": "https://foreign.test"}, {**command_headers, "Host": "foreign.test"}):
                code, _value, _headers = await api("/api/units/idle/start", "POST", {}, extra)
                assert code == 403
            check("https_cookie_origin_host_and_csrf_enforced")

            # Run Chromium in its own finite, no-force-kill transient cgroup.
            args = ["systemd-run", "--user", "--quiet", "--unit=" + browser_name,
                    "--property=Type=exec", "--property=ExitType=cgroup", "--property=Restart=no",
                    "--property=SendSIGKILL=no", "--property=KillMode=control-group", "--property=TimeoutStopSec=5",
                    "--property=RuntimeMaxSec=" + ("300" if av_native else "180" if ros or parameters or observations or snapshots or imu or lidar or fiducials or actions or commands else "120"), "/snap/bin/chromium", "--headless=new", "--disable-gpu", "--no-sandbox",
                    "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
                    "--disable-component-update", "--disable-sync", "--remote-debugging-port=" + str(debug_port),
                    "--remote-debugging-address=127.0.0.1", "--user-data-dir=" + str(browser_profile),
                    "--ignore-certificate-errors-spki-list=" + fingerprint, "about:blank"]
            await asyncio.to_thread(subprocess.run, args, check=True, capture_output=True, timeout=10)
            browser_started = True
            async with aiohttp.ClientSession() as devtools:
                async def endpoint():
                    async with devtools.get(f"http://127.0.0.1:{debug_port}/json") as response:
                        values = await response.json()
                        return next((value["webSocketDebuggerUrl"] for value in values if value.get("type") == "page"), None)
                websocket = await wait_for(endpoint, seconds=25)
                for process in Path("/proc").glob("[0-9]*/cmdline"):
                    try:
                        if any(str(browser_profile).encode() in argument for argument in process.read_bytes().split(b"\0")):
                            group = (process.parent / "cgroup").read_text().strip().split("::", 1)[1]
                            path = (Path("/sys/fs/cgroup") / group.lstrip("/")).resolve()
                            assert path.is_relative_to("/sys/fs/cgroup") and path.name.endswith((".scope", ".service"))
                            browser_groups.add(path)
                    except (FileNotFoundError, PermissionError):
                        pass
                assert browser_groups, "Chromium must use the declared isolated profile"
                async with devtools.ws_connect(websocket) as connection:
                    cdp = CDP(connection)
                    await cdp.call("Runtime.enable")
                    await cdp.call("Emulation.setDeviceMetricsOverride", width=1200, height=800, deviceScaleFactor=1, mobile=False)
                    await cdp.call("Page.navigate", url=origin)
                    await wait_for(lambda: cdp.evaluate('!!document.querySelector("tr[data-unit=idle]")'))
                    assert await cdp.evaluate('document.querySelector("button[data-command=start]").disabled')
                    await cdp.evaluate('document.getElementById("passphrase").value=' + json.dumps(passphrase) + '; document.querySelector("#unlock button").click(); true')
                    await wait_for(lambda: cdp.evaluate('document.getElementById("unlock").hidden'))
                    browser_session = await cdp.evaluate('fetch("/api/session").then(r=>r.json()).then(s=>s.session_id)')
                    assert browser_session != session["session_id"]
                    await cdp.evaluate('document.querySelector("button[data-command=start]").click(); true')
                    await wait_for(lambda: cdp.evaluate('document.querySelector("tr[data-unit=idle] td:nth-child(2)").textContent.includes("running")'))
                    await cdp.evaluate('[...document.querySelectorAll("tr[data-unit=idle] button")].find(b=>b.textContent==="Logs").click(); true')
                    await cdp.evaluate('document.getElementById("follow-logs").checked=true; true')
                    log_ready = "Server listening on port" if foxglove else "ready"
                    await wait_for(lambda: cdp.evaluate('document.getElementById("logs").textContent.includes(' + json.dumps(log_ready) + ')'))
                    if ros:
                        from ros_browser_checks import exercise
                        await exercise(cdp, api, local, area, check, command_headers, client, origin)
                    if parameters:
                        from parameter_browser_checks import exercise
                        await exercise(cdp, api, local, area, check, command_headers, client, origin)
                    if observations:
                        from observations_browser_checks import exercise
                        await exercise(cdp, local, area, check, client, origin)
                    if snapshots:
                        from snapshot_browser_checks import exercise
                        await exercise(cdp, local, area, check, origin)
                    if imu:
                        from imu_browser_checks import exercise
                        await exercise(cdp, local, area, check, client, origin)
                    if lidar:
                        from lidar_browser_checks import exercise
                        await exercise(cdp, local, area, check, client, origin)
                    if fiducials:
                        from fiducials_browser_checks import exercise
                        await exercise(cdp, local, area, check, client, origin)
                    if resources:
                        from resource_browser_checks import exercise
                        await exercise(cdp, local, area, check, client, origin, restart_manager)
                    if actions:
                        from action_browser_checks import exercise
                        await exercise(cdp, api, local, area, check, command_headers, origin, passphrase)
                    if commands:
                        from command_browser_checks import exercise
                        await exercise(cdp, local, area, check, passphrase)
                    if foxglove:
                        from foxglove_browser_checks import exercise
                        await exercise(cdp, local, area, check)
                    if delivery:
                        from delivery_browser_checks import exercise
                        await exercise(cdp, devtools, debug_port, origin, local, area, check)
                    if av_native:
                        async def restart_web():
                            nonlocal web_process
                            web_process.terminate()
                            await asyncio.to_thread(web_process.wait, timeout=10)
                            web_process = launch_web()
                            await wait_for(web_ready)
                        from av_browser_checks import exercise
                        await exercise(cdp, devtools, debug_port, origin, local, area, check,
                                       api, command_headers, passphrase, restart_manager, restart_web)
                    shot = await cdp.call("Page.captureScreenshot", format="png")
                    (area / "browser-running.png").write_bytes(base64.b64decode(shot["data"]))
                    # Optional views change stream demand and briefly disable
                    # commands while the current selection reconnects. A click
                    # on a disabled button is silently ignored by the browser.
                    await wait_for(lambda: cdp.evaluate('''(() => {
                      const button = document.querySelector('tr[data-unit=idle] button[data-command=stop]');
                      if (!button || button.disabled) return false;
                      button.click(); return true;
                    })()'''))
                    await wait_for(lambda: cdp.evaluate('document.querySelector("tr[data-unit=idle] td:nth-child(2)").textContent.includes("exited")'))
                    if observations:
                        await wait_for(lambda: cdp.evaluate('document.querySelectorAll(".cone-points path").length === 0 && document.querySelector(".health-card").dataset.observationState === "unavailable"'))
                        check("stop_clears_live_geometry_and_positive_health")
                    if snapshots:
                        await wait_for(lambda: cdp.evaluate('document.querySelector(".snapshot-card img").hidden'))
                        check("stop_clears_snapshot")
                    if imu:
                        await wait_for(lambda: cdp.evaluate('document.querySelectorAll(".imu-sensor-axes [data-axis]").length === 0 && document.querySelector(\'[data-ros-topic="imu-health"]\').dataset.observationState === "unavailable"'))
                        check("stop_clears_imu_pose_and_provider_health")
                    await cdp.evaluate('document.getElementById("lock").click(); true')
                    await wait_for(lambda: cdp.evaluate('!document.getElementById("unlock").hidden'))
                    assert await cdp.evaluate('document.querySelector("button[data-command=start]").disabled')
                    if parameters:
                        assert await cdp.evaluate('document.querySelector(\'[data-parameter="title"] .parameter-name input\').disabled')
                    assert not cdp.exceptions
                    check("real_chromium_unlock_start_status_logs_stop_lock", independent_session=True, javascript_exceptions=cdp.exceptions)

                    status = await local("status", fresh=True)
                    payload = dict(request_id=str(uuid.uuid4()), expires_at_ms=int(time.time() * 1000) + 10000, config_revision=status["config_revision"])
                    code, operation, _headers = await api("/api/units/idle/start", "POST", payload, command_headers)
                    assert code == 202
                    async def running():
                        value = (await local("status", fresh=True))["units"][0]
                        return value if value["lifecycle"] == "running" else None
                    live = await wait_for(running)
                    if ros or parameters or observations or snapshots or imu or lidar or fiducials or actions or commands:
                        async def provider_started():
                            return (area / "provider-events.jsonl").read_text().count('"event": "ready"') >= 2
                        await wait_for(provider_started)
                    web_process.terminate()
                    await asyncio.to_thread(web_process.wait, timeout=10)
                    if resources:
                        from resource_browser_checks import cli_without_web
                        await cli_without_web(state, local, check)
                    current = await local("status", fresh=True)
                    assert current["units"][0]["run_id"] == live["run_id"] and current["units"][0]["main_pid"] == live["main_pid"]
                    cli = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "robot_runtime.cli", "--state", str(state), "stop", "idle", "--expected-run", live["run_id"]],
                                                  capture_output=True, text=True, check=True, timeout=10)
                    assert json.loads(cli.stdout)["delivery"] == "acknowledged"
                    check("cli_operates_with_web_down_and_workload_survives_web_exit")
                    web_process = launch_web()
                    await wait_for(web_ready)
                    code, _value, _headers = await api("/api/units/idle/start", "POST", payload, command_headers)
                    assert code == 401
                    check("web_restart_revokes_sessions")

                    responses = []
                    try:
                        for _ in range(5):
                            response = await client.get(origin + "/api/events")
                            assert response.status == 200
                            line = await asyncio.wait_for(response.content.readline(), 3)
                            assert line.startswith(b"data: ") and json.loads(line[6:])["fresh"]
                            responses.append(response)
                        check("five_concurrent_observation_connections")
                    finally:
                        for response in responses:
                            response.close()
                    if ros:
                        # Fill the stream budget, then reload. HTTP admission
                        # errors leave native EventSource CLOSED rather than
                        # retrying, so the shared read-only client must recover.
                        await cdp.evaluate('pageClients.runtime.disconnect(); true')
                        responses = []
                        async def reserve():
                            response = await client.get(origin + "/api/events")
                            if response.status == 200:
                                responses.append(response)
                                return True
                            assert response.status == 409
                            response.close()
                            return False
                        try:
                            for _ in range(8): await wait_for(reserve)
                            before = await local("status", fresh=True)
                            await cdp.call("Page.reload")
                            await wait_for(lambda: cdp.evaluate('typeof pageClients !== "undefined" && pageClients?.runtime.state.connection === "disconnected" && state === null'))
                        finally:
                            for response in responses: response.close()
                        await wait_for(lambda: cdp.evaluate('state !== null && fresh() && pageClients.runtime.state.connection === "open"'), seconds=20)
                        after = await local("status", fresh=True)
                        assert [(u["unit_id"],u["run_id"]) for u in before["units"]] == [(u["unit_id"],u["run_id"]) for u in after["units"]]
                        check("closed_observation_stream_recovers_after_admission_limit_without_new_runs")
        report["passed"] = True
    except BaseException as exc:
        report.update(passed=False, error=repr(exc))
        with contextlib.suppress(Exception):
            report["browser_failure"] = dict(exceptions=cdp.exceptions, state=await cdp.evaluate(
                '({resourceText:document.getElementById("resource-status")?.textContent,'
                'runtime:pageClients?.runtime.state.connection, resources:pageClients?.resources.state})'))
        with contextlib.suppress(Exception):
            report["ros_failure_state"] = await local("ros_status")
        raise
    finally:
        errors = []
        if browser_started:
            try:
                # Snap may place Chromium in its own application scope. Close the
                # exact DevTools browser, then verify every observed cgroup empty.
                async with aiohttp.ClientSession() as closing:
                    async with closing.get(f"http://127.0.0.1:{debug_port}/json/version") as response:
                        browser_url = (await response.json())["webSocketDebuggerUrl"]
                    async with closing.ws_connect(browser_url) as websocket:
                        await websocket.send_json(dict(id=1, method="Browser.close"))
                        with contextlib.suppress(aiohttp.ClientError, asyncio.TimeoutError):
                            await websocket.receive(timeout=3)
                async def browser_empty():
                    return all(not path.exists() or not (path / "cgroup.procs").read_text().strip() for path in browser_groups)
                await wait_for(browser_empty, seconds=10)
                cgroup = subprocess.check_output(["systemctl", "--user", "show", browser_name, "-p", "ControlGroup", "--value"], text=True).strip()
                loaded = subprocess.check_output(["systemctl", "--user", "show", browser_name, "-p", "LoadState", "--value"], text=True).strip()
                # Snap's launcher can exit and its transient wrapper be collected
                # before the actual browser scope is closed and verified above.
                if loaded != "not-found":
                    await asyncio.to_thread(subprocess.run, ["systemctl", "--user", "stop", browser_name], check=True, capture_output=True, timeout=15)
                if cgroup:
                    path = Path("/sys/fs/cgroup") / cgroup.lstrip("/")
                    assert not path.exists() or not (path / "cgroup.procs").read_text().strip()
                await asyncio.to_thread(subprocess.run, ["systemctl", "--user", "reset-failed", browser_name], capture_output=True, timeout=5)
                if browser_profile.exists():
                    assert browser_profile.parent == Path.home() / "snap/chromium/common" and browser_profile.resolve() == browser_profile
                    shutil.rmtree(browser_profile)
            except Exception as exc:
                errors.append("browser cleanup: " + repr(exc))
        if web_process and web_process.poll() is None:
            web_process.terminate()
            await asyncio.to_thread(web_process.wait, timeout=10)
        try:
            current = await local("status", fresh=True)
            for unit in current["units"]:
                if unit["blocked"] and not unit.get("stop_request_id"):
                    await local("stop", unit_id=unit["unit_id"], request_id=str(uuid.uuid4()), expires_at_ms=int(time.time() * 1000) + 10000,
                                config_revision=current["config_revision"], expected_run=unit["run_id"])
            async def empty():
                return not any(unit["blocked"] for unit in (await local("status", fresh=True))["units"])
            await wait_for(empty)
            report["final_status"] = await local("status", fresh=True)
        except Exception as exc:
            errors.append("runtime cleanup: " + repr(exc))
        manager.terminate()
        await asyncio.to_thread(manager.wait, timeout=10)
        manager_log.close()
        web_log.close()
        report["cleanup_errors"] = errors
        (area / "report.json").write_text(json.dumps(report, indent=2))
        (root / "latest-web.txt").write_text(area.name)
        if errors:
            raise RuntimeError("Inspect browser cleanup report")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--ros", action="store_true")
    parser.add_argument("--parameters", action="store_true")
    parser.add_argument("--observations", action="store_true")
    parser.add_argument("--snapshots", action="store_true")
    parser.add_argument("--imu", action="store_true")
    parser.add_argument('--lidar', action='store_true')
    parser.add_argument('--fiducials', action='store_true')
    parser.add_argument('--resources', action='store_true')
    parser.add_argument('--actions', action='store_true')
    parser.add_argument('--commands', action='store_true')
    parser.add_argument("--foxglove", action="store_true")
    parser.add_argument("--delivery", action="store_true", help="Use with --ros to check three tabs and minimal compositions")
    parser.add_argument("--av-native", type=Path, help="Opt in to native video/input checks using this prepared prefix")
    args = parser.parse_args()
    if args.delivery and not args.ros:
        parser.error("--delivery requires --ros")
    asyncio.run(main(args.root, ros=args.ros, parameters=args.parameters, observations=args.observations, snapshots=args.snapshots, imu=args.imu, lidar=args.lidar, fiducials=args.fiducials, resources=args.resources, actions=args.actions, commands=args.commands, foxglove=args.foxglove, delivery=args.delivery, av_native=args.av_native))
