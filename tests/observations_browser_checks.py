"""Actual Chromium checks with the native, hardware-free observation fixture."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import asyncio
import base64
import json
import os

from integration_browser import wait_for


async def exercise(cdp, local, area, check, client, origin):
    def control(**values):
        temporary = area / "control.tmp"
        temporary.write_text(json.dumps(values))
        os.replace(temporary, area / "control.json")
    async def topic(key="cones"):
        return next(value for value in (await local("ros_status"))["topics"] if value["id"] == key)
    async def subscribers(count):
        value = json.loads((area / "provider-status.json").read_text())
        return value["cone_subscribers"] == value["health_subscribers"] == count
    async def sample_status(status):
        return (await topic())["status"] == status
    async def state(key, value):
        return await cdp.evaluate(f'document.querySelector("[data-ros-topic={key}]")?.dataset.observationState === {json.dumps(value)}')
    async def fresh():
        return await state("cones", "fresh")
    async def absent():
        return await cdp.evaluate('document.querySelectorAll(".cone-points path").length === 0')

    await wait_for(lambda: cdp.evaluate('!!document.querySelector(".cone-plot")'))
    await wait_for(lambda: subscribers(0))
    await cdp.evaluate('document.getElementById("ros-observe").click(); true')
    await wait_for(fresh)
    await wait_for(lambda: state("health", "progressing"))
    points = await cdp.evaluate('[...document.querySelectorAll(".cone-points path")].map(p=>({...p.dataset}))')
    assert len(points) == 2 and float(points[0]["screenX"]) < 300 < float(points[1]["screenX"])
    assert float(points[1]["screenY"]) < float(points[0]["screenY"]) < 550
    value = await topic()
    assert isinstance(value["data"]["acquisition_timestamp_ns"], str) and int(value["data"]["acquisition_timestamp_ns"]) > 2**53
    assert value["data"]["detections"][0]["z"] == 0.2
    check("native_detections_preserve_xyz_lossless_acquisition_time_and_requested_plot_axes", sample=value, plotted=points)

    control(count=0)
    await wait_for(lambda: state("cones", "empty"))
    assert await absent()
    control(count=25)
    await wait_for(lambda: cdp.evaluate('document.querySelector(".cone-summary").textContent.includes("truncated to first 20 of 25")'))
    assert await cdp.evaluate('document.querySelectorAll(".cone-points path").length') == 20
    check("fresh_empty_clears_prior_cones_and_truncation_is_explicit")

    for settings, status in ((dict(frame="wrong_frame"), "frame_mismatch"), (dict(stamp_offset=10), "timestamp_in_future"),
                             (dict(count=2000), "sample_too_large")):
        control(**settings)
        await wait_for(lambda: sample_status(status))
        await wait_for(absent)
    control(freeze_stamp=True)
    await wait_for(fresh)
    await wait_for(lambda: sample_status("stale_source"))
    await wait_for(absent)
    value = await topic()
    assert value["age_seconds"] < 0.4 and not value["fresh"] and value["acquisition_age_seconds"] > 0.6
    check("wrong_frame_future_time_raw_limit_and_republished_old_acquisitions_never_show_live", stale_source=value)

    control()
    await wait_for(fresh)
    control(publish=False)
    await wait_for(lambda: sample_status("stale"))
    await wait_for(absent)
    control(freeze_health=True)
    await wait_for(fresh)
    await wait_for(lambda: state("health", "awaiting_progress"))
    assert (await topic("health"))["fresh"]
    control(level=1)
    await wait_for(lambda: state("health", "warning"))
    control(duplicate_status=True)
    await wait_for(lambda: state("health", "unavailable"))
    control(publish_health=False)
    await wait_for(lambda: state("health", "unavailable"))
    control()
    await wait_for(lambda: state("health", "progressing"))
    check("fresh_diagnostics_need_recent_counter_progress_and_warnings_or_missing_evidence_clear_positive_health")

    await cdp.evaluate('document.getElementById("ros-panel").scrollIntoView(); true')
    (area / "browser-observations.png").write_bytes(base64.b64decode((await cdp.call("Page.captureScreenshot", format="png"))["data"]))
    generation = (await topic())["stream_generation"]
    responses = []
    try:
        for _ in range(5):
            response = await client.get(origin + "/api/ros/events?topic=cones&topic=health")
            assert response.status == 200
            responses.append(response)
            await asyncio.wait_for(response.content.readline(), 5)
        await wait_for(lambda: subscribers(1))
        check("five_https_viewers_and_browser_share_one_subscription_per_topic")
    finally:
        for response in responses:
            response.close()
    await cdp.evaluate('document.getElementById("ros-observe").click(); true')
    await wait_for(lambda: subscribers(0), seconds=12)
    assert await absent()
    await cdp.evaluate('document.getElementById("ros-observe").click(); true')
    await wait_for(fresh)
    assert (await topic())["stream_generation"] != generation
    await cdp.evaluate('Object.defineProperty(document,"hidden",{configurable:true,value:true}); document.dispatchEvent(new Event("visibilitychange")); true')
    await wait_for(lambda: subscribers(0), seconds=12)
    assert await absent()
    await cdp.evaluate('delete document.hidden; document.dispatchEvent(new Event("visibilitychange")); true')
    await wait_for(fresh)
    check("pause_and_simulated_hidden_page_release_demand_and_resume_new_stream_without_cached_positions")
    await cdp.evaluate('window.scrollTo(0,0); true')
