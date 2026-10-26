"""Packaged browser-client checks with private synthetic HTTP/SSE, run on jah."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import argparse
import asyncio
import contextlib
import hashlib
import importlib.metadata
import importlib.resources
import json
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web

from integration_browser import CDP, port, wait_for


async def main(root):
    root = root.resolve()
    assert root.parent == Path("/tmp") and re.fullmatch(r"robot-runtime-slice-[0-9a-f]{12}", root.name)
    token = uuid.uuid4().hex[:10]
    area = root / ("clients-" + token)
    area.mkdir(mode=0o700)
    profile_parent = Path.home() / "snap/chromium/common"
    browser_profile = profile_parent / ("robot-runtime-test-" + token)
    assert not browser_profile.exists()
    unit = "drt-clients-" + token
    assets = importlib.resources.files("robot_runtime") / "static"
    module_names = [f"client-{name}.mjs" for name in ("core", "http", "sse", "stream", "multiplex", "page")]
    module_names += ['launch-control.mjs']
    module_names += ['av/' + name for name in ('core.js', 'runtime-api.js', 'media-client.js', 'freshness.js', 'control-client.js', 'gamepad-input.js', 'gamepad-art.js',
        'components/gamepad-view.js', 'components/control-panel.js', 'components/video-view.js', 'components/source-controls.js')]
    metrics = {"active": 0, "selections": [], "commands": 0}
    report = {
        "passed": False, "cleanup_errors": [], "synthetic": True,
        "package_version": importlib.metadata.version("robot-runtime"),
        "package_location": str(assets), "python": sys.version,
        "aiohttp": aiohttp.__version__,
        "module_sha256": {name: hashlib.sha256((assets / name).read_bytes()).hexdigest() for name in module_names},
    }

    async def index(request):
        return web.Response(text="<!doctype html><title>Browser client fixture</title>", content_type="text/html")

    async def module(request):
        name = request.match_info["name"]
        if name not in module_names:
            raise web.HTTPNotFound()
        return web.Response(body=(assets / name).read_bytes(), content_type="application/javascript")

    async def suite(request):
        return web.Response(body=Path(__file__).with_name("browser_clients.mjs").read_bytes(), content_type="application/javascript")

    async def av_suite(request):
        return web.Response(body=Path(__file__).with_name("browser_av_clients.mjs").read_bytes(), content_type="application/javascript")

    async def stream(request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await response.prepare(request)
        metrics["active"] += 1
        metrics["selections"].append(request.query.getall("topic", []))
        snapshot = {"api_version": 1, "available": True, "topics": [{
            "id": "cones", "fresh": True, "status": "current", "age_seconds": 0,
            "stale_after_seconds": 0.15, "data": {"points": [], "stamp_ns": "1792497600123456789"},
        }]}
        try:
            await response.write(("data: " + json.dumps(snapshot) + "\n\n").encode())
            while request.transport is not None and not request.transport.is_closing():
                await asyncio.sleep(0.05)
                await response.write(b": heartbeat\n\n")
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            metrics["active"] -= 1
        return response

    async def read_metrics(request):
        return web.json_response(metrics)

    async def command(request):
        metrics["commands"] += 1
        return web.json_response({"body": await request.json(), "csrf": request.headers.get("X-CSRF-Token")})

    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/assets/{name:.+}", module), web.get("/suite.mjs", suite), web.get("/av-suite.mjs", av_suite),
                    web.get("/api/ros/events", stream), web.get("/fixture/metrics", read_metrics),
                    web.post("/fixture/command", command)])
    runner = web.AppRunner(app)
    browser_started = False
    cgroup = None
    cdp = None
    connection = None
    try:
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        origin = f"http://127.0.0.1:{runner.addresses[0][1]}"
        debug_port = port()
        subprocess.run([
            "systemd-run", "--user", "--quiet", "--unit=" + unit,
            "--property=Type=exec", "--property=ExitType=cgroup", "--property=Restart=no",
            "--property=SendSIGKILL=no", "--property=KillMode=control-group",
            "--property=TimeoutStopSec=5", "--property=RuntimeMaxSec=120",
            "/snap/bin/chromium", "--headless=new", "--disable-gpu", "--no-sandbox",
            "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
            "--disable-background-networking", "--disable-component-update", "--disable-sync",
            "--remote-debugging-port=" + str(debug_port), "--remote-debugging-address=127.0.0.1",
            "--user-data-dir=" + str(browser_profile), "about:blank",
        ], check=True, timeout=15)
        browser_started = True
        group = subprocess.check_output(["systemctl", "--user", "show", unit, "--property=ControlGroup", "--value"], text=True).strip()
        assert group and group.endswith("/" + unit + ".service")
        cgroup = (Path("/sys/fs/cgroup") / group.lstrip("/")).resolve()
        assert cgroup.is_relative_to("/sys/fs/cgroup")
        async with aiohttp.ClientSession() as devtools:
            async def endpoint():
                async with devtools.get(f"http://127.0.0.1:{debug_port}/json/list") as response:
                    return next((value["webSocketDebuggerUrl"] for value in await response.json() if value.get("type") == "page"), None)

            websocket = await wait_for(endpoint, seconds=25)
            connection = await devtools.ws_connect(websocket)
            cdp = CDP(connection)
            try:
                report["browser"] = await cdp.call("Browser.getVersion")
                await cdp.call("Runtime.enable")
                await cdp.call("Page.navigate", url=origin)
                await wait_for(lambda: cdp.evaluate("location.origin === " + json.dumps(origin)))
                result = await cdp.evaluate("import('/suite.mjs').then(suite => suite.run())")
                report.update(result)
                report["browser_exceptions"] = cdp.exceptions
                report["fixture"] = dict(metrics)
                assert result["passed"] and not cdp.exceptions, "Browser client checks failed"
                assert metrics["active"] == 0 and metrics["commands"] == 1, metrics
            finally:
                with contextlib.suppress(Exception):
                    await cdp.call("Browser.close")
                await connection.close()
    except Exception as error:
        report["passed"] = False
        report["error"] = repr(error)
    finally:
        if browser_started:
            stopped = subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, text=True, timeout=15)
            if stopped.returncode:
                report["cleanup_errors"].append(stopped.stderr.strip())
            if cgroup:
                async def empty():
                    return not cgroup.exists() or "populated 1" not in (cgroup / "cgroup.events").read_text()
                try:
                    await wait_for(empty, seconds=10)
                except Exception as error:
                    report["cleanup_errors"].append("Browser cgroup: " + repr(error))
            subprocess.run(["systemctl", "--user", "reset-failed", unit], capture_output=True, timeout=10)
        await runner.cleanup()
        if browser_profile.exists() and not report["cleanup_errors"]:
            assert not browser_profile.is_symlink() and browser_profile.resolve().parent == profile_parent.resolve()
            shutil.rmtree(browser_profile)
        report["cleanup"] = {"browser_cgroup_empty": cgroup is None or not cgroup.exists() or
                             "populated 1" not in (cgroup / "cgroup.events").read_text(),
                             "profile_removed": not browser_profile.exists(), "active_streams": metrics["active"]}
        report["passed"] = report["passed"] and not report["cleanup_errors"] and metrics["active"] == 0
        (area / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"report": str(area / "report.json"), "passed": report["passed"],
                          "checks": report.get("checks"), "cleanup_errors": report["cleanup_errors"]}, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.root)))
