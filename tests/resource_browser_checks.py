"""Native resource collection and Chromium demand lifecycle on isolated units."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import asyncio
import base64
import json
import sys

from integration_browser import wait_for


async def exercise(cdp, local, area, check, client, origin, restart_manager):
    async def consumers(number):
        return (await local("resource_status"))["consumers"] == number

    async def usable():
        value = await local("resource_status")
        sample = value["latest"]
        if not sample or not value["fresh"]:
            return False
        unit = sample["units"][0]
        return value if sample["host"].get("cpu_percent") is not None and unit["cpu_percent"] is not None and unit["rss_bytes"] else False

    before = await local("status", fresh=True)
    untouched = await local("resource_status", history=True)
    assert not untouched["collecting"] and untouched["sequence"] == 0 and untouched["history"] == []
    async with client.get(origin + "/api/resources?history=true") as response:
        assert response.status == 200 and not (await response.json())["collecting"]
    await cdp.evaluate('document.getElementById("resource-panel").open=true; true')
    first = await wait_for(usable)
    await wait_for(lambda: cdp.evaluate('document.getElementById("resource-status").textContent.includes("Monitoring live") && !document.querySelector("#resource-units td:nth-child(2)").textContent.includes("Unavailable")'))
    assert first["consumers"] == 1
    assert first["latest"]["units"][0]["run_id"] == before["units"][0]["run_id"]
    assert first["latest"]["units"][0]["memory_current_bytes"] > 0
    async def gpu_ready():
        value = await local("resource_status")
        gpu = value["latest"]["gpu"]
        return gpu if gpu["activity_percent"] is not None and gpu["temperature_c"] is not None else None
    gpu = await wait_for(gpu_ready)
    check("cached_reads_and_closed_panel_are_idle_then_native_host_workload_gpu_metrics_arrive", sample=first, gpu=gpu)

    responses = []
    try:
        for _ in range(4):
            response = await client.get(origin + "/api/resources/events")
            assert response.status == 200
            responses.append(response)
            line = await asyncio.wait_for(response.content.readline(), 3)
            initial = json.loads(line[6:])
            assert "history" in initial and initial["generation"] == first["generation"]
        await asyncio.sleep(2)
        assert await consumers(1)
        current = await local("resource_status")
        assert current["sequence"] - first["sequence"] < 12
        check("five_browser_viewers_share_one_manager_collector", consumers=current["consumers"])
    finally:
        for response in responses:
            response.close()
    await cdp.evaluate('document.getElementById("resource-panel").open=false; true')
    await wait_for(lambda: consumers(0))
    paused = await local("resource_status", history=True)
    await asyncio.sleep(4)
    idle = await local("resource_status", history=True)
    assert not idle["fresh"] and idle["sequence"] == paused["sequence"]
    assert idle["history"] and len(idle["history"]) <= 600
    await cdp.evaluate('document.getElementById("resource-panel").open=true; true')
    await wait_for(usable)
    resumed = await local("resource_status", history=True)
    assert any(b[1] - a[1] > 3 for a, b in zip(resumed["history"], resumed["history"][1:]))
    check("last_viewer_release_stops_collection_and_resume_retains_a_real_history_gap")

    await cdp.evaluate('Object.defineProperty(document,"hidden",{configurable:true,get:()=>true}); document.dispatchEvent(new Event("visibilitychange")); true')
    await wait_for(lambda: consumers(0))
    await cdp.evaluate('delete document.hidden; document.dispatchEvent(new Event("visibilitychange")); true')
    await wait_for(usable)
    generation = (await local("resource_status"))["generation"]
    await restart_manager()
    restored = await wait_for(usable, seconds=25)
    assert restored["generation"] != generation
    assert restored["sequence"] < 15
    after = await local("status", fresh=True)
    assert after["units"][0]["run_id"] == before["units"][0]["run_id"]
    await wait_for(lambda: cdp.evaluate('document.getElementById("resource-status").textContent.includes("Monitoring live")'))
    check("hidden_page_releases_demand_and_manager_restart_clears_history_without_restarting_workloads")
    await cdp.evaluate('document.getElementById("resource-panel").scrollIntoView(); true')
    shot = await cdp.call("Page.captureScreenshot", format="png")
    (area / "resource-monitoring.png").write_bytes(base64.b64decode(shot["data"]))
    await cdp.evaluate('document.getElementById("resource-panel").open=false; true')
    await wait_for(lambda: consumers(0))


async def cli_without_web(state, local, check):
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "robot_runtime.cli", "--state", str(state), "resources", "--watch",
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        initial = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
        assert "history" in initial
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            value = json.loads(await asyncio.wait_for(process.stdout.readline(), 4))
            if value["fresh"]:
                break
        assert value["fresh"] and value["consumers"] == 1
        check("standalone_cli_resource_watch_works_with_web_stopped")
    finally:
        process.terminate()
        await asyncio.wait_for(process.wait(), 5)
    async def released():
        return not (await local("resource_status"))["collecting"]
    await wait_for(released)
