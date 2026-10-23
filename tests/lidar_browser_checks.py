"""Native LaserScan transport and independent layers in actual Chromium."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import asyncio
import base64
import json
import os

from integration_browser import wait_for


async def exercise(cdp, local, area, check, client, origin):
    def control(role="lidar", **values):
        temporary = area / (role+"-control.tmp")
        temporary.write_text(json.dumps(values))
        os.replace(temporary,area / (role+"-control.json"))
    async def topic(key="scan"):
        return next(item for item in (await local("ros_status"))["topics"] if item["id"] == key)
    async def unit(key="idle"):
        return next(item for item in (await local("status", fresh=True))["units"] if item["unit_id"] == key)
    async def state(key, expected):
        return await cdp.evaluate(f'document.querySelector("[data-ros-topic={key}]")?.dataset.observationState === {json.dumps(expected)}')
    async def status(expected): return (await topic())["status"] == expected
    async def subscribers(cones, lidar):
        return all(json.loads((area / (role+"-status.json")).read_text())["subscribers"] == count for role,count in (("cones",cones),("lidar",lidar)))
    async def both(): return await state("scan","fresh") and await state("cones","fresh")
    await cdp.evaluate('document.querySelector("tr[data-unit=peer] button[data-command=start]").click(); true')
    await wait_for(lambda: subscribers(0,0))
    await cdp.evaluate('document.getElementById("ros-observe").click(); true')
    await wait_for(both)
    cone_run, lidar_run = (await unit())["run_id"], (await unit("peer"))["run_id"]
    assert await cdp.evaluate('document.querySelectorAll(".spatial-plot").length === 1 && document.querySelectorAll(".lidar-points circle").length === 360 && document.querySelectorAll(".cone-points path").length === 2')
    value = await topic()
    assert value["data"]["points"][0] == [2.2,0.,.12,0]
    assert value["data"]["valid_count"] == 720 and value["data"]["downsampled"]
    assert await cdp.evaluate('document.querySelector("[data-ros-topic=scan]").textContent.includes("estimated scan start")')
    check("native_scan_and_cones_share_metric_frame_with_full_scan_sampling_and_mount", sample=value)
    await cdp.evaluate('[...document.querySelectorAll(".spatial-card button")].find(b=>b.textContent==="Forward").click(); true')
    count = await cdp.evaluate('document.querySelectorAll(".lidar-points circle").length')
    assert 0 < count < 360
    await cdp.evaluate('[...document.querySelectorAll(".spatial-card button")].find(b=>b.textContent==="Zoom in").click(); true')
    assert await cdp.evaluate('document.querySelector(".spatial-extent").textContent.includes("4.0")')
    await cdp.evaluate('[...document.querySelectorAll(".spatial-card button")].find(b=>b.textContent==="Surround").click(); true')
    for key, counts in (("scan",(1,0)),("cones",(0,1))):
        await cdp.evaluate(f'document.querySelector("[data-ros-topic={key}] input").click(); true')
        await wait_for(lambda: subscribers(*counts), seconds=12)
        assert await state(key,"unavailable")
        await wait_for(lambda: state("cones" if key == "scan" else "scan","fresh"))
        await cdp.evaluate(f'document.querySelector("[data-ros-topic={key}] input").click(); true')
        await wait_for(both)
    assert (await unit())["run_id"] == cone_run and (await unit("peer"))["run_id"] == lidar_run
    check("presets_zoom_and_independent_layer_toggles_preserve_runs_and_release_only_disabled_demand")
    control(empty=True)
    await wait_for(lambda: state("scan","empty"))
    assert await state("cones","fresh")
    assert await cdp.evaluate('document.querySelectorAll(".lidar-points circle").length === 0')
    control(partial=True)
    async def partial(): return (await topic()).get("data",{}).get("valid_count") == 717
    await wait_for(partial)
    for settings, expected in ((dict(frame="map"),"frame_mismatch"),(dict(bad_angles=True),"invalid_sample"),
        (dict(stamp_offset=10),"timestamp_in_future"),(dict(count=20000),"sample_too_large"),(dict(freeze_stamp=True),"stale_source"),
        (dict(publish=False),"stale"),(dict(collide=True),"source_ambiguous")):
        control(**settings)
        await wait_for(lambda: status(expected))
        await wait_for(lambda: state("scan","unavailable"))
        assert await state("cones","fresh")
        control()
        await wait_for(both)
    check("empty_partial_invalid_old_oversized_and_ambiguous_scans_clear_only_lidar")
    responses=[]
    try:
        for _ in range(5):
            response = await client.get(origin+"/api/ros/events?topic=scan&topic=cones")
            assert response.status == 200
            responses.append(response)
            await asyncio.wait_for(response.content.readline(),5)
        await wait_for(lambda: subscribers(1,1))
    finally:
        for response in responses: response.close()
    await cdp.evaluate('Object.defineProperty(document,"hidden",{configurable:true,value:true}); document.dispatchEvent(new Event("visibilitychange")); true')
    await wait_for(lambda: subscribers(0,0),seconds=12)
    assert await cdp.evaluate('document.querySelectorAll(".lidar-points circle,.cone-points path").length === 0')
    await cdp.evaluate('delete document.hidden; document.dispatchEvent(new Event("visibilitychange")); true')
    await wait_for(both)
    check("multiple_viewers_share_subscriptions_and_hidden_page_releases_both_layers")
    for width in (1400,900):
        await cdp.call("Emulation.setDeviceMetricsOverride", width=width,height=1000,deviceScaleFactor=1,mobile=False)
        await cdp.evaluate('document.querySelector(".spatial-card").scrollIntoView(); true')
        assert await cdp.evaluate('document.querySelector(".spatial-card").getBoundingClientRect().right <= window.innerWidth')
        (area / f"browser-lidar-{width}.png").write_bytes(base64.b64decode((await cdp.call("Page.captureScreenshot",format="png"))["data"]))
    control(exit=True)
    async def failed(): return (await unit("peer"))["lifecycle"] == "failed"
    await wait_for(failed)
    await wait_for(lambda: state("scan","unavailable"))
    assert await state("cones","fresh") and (await unit())["run_id"] == cone_run
    check("lidar_provider_failure_preserves_camera_run_and_layer")
    control()
    await cdp.evaluate('window.scrollTo(0,0); true')
