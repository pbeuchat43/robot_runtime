"""Native ROS and real Chromium checks for IMU meaning and unit independence."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import asyncio
import base64
import json
import os

from integration_browser import wait_for


async def exercise(cdp, local, area, check, client, origin):
    def control(role="imu", **values):
        temporary = area / (role + "-control.tmp")
        temporary.write_text(json.dumps(values))
        os.replace(temporary, area / (role + "-control.json"))
    async def topic(key="imu"):
        return next(value for value in (await local("ros_status"))["topics"] if value["id"] == key)
    async def unit(key):
        return next(value for value in (await local("status", fresh=True))["units"] if value["unit_id"] == key)
    async def state(key, expected):
        return await cdp.evaluate(f'document.querySelector(\'[data-ros-topic="{key}"]\')?.dataset.observationState === {json.dumps(expected)}')
    async def fresh():
        return await state("imu", "fresh")
    async def sample_status(expected):
        return (await topic())["status"] == expected
    async def no_pose():
        return await cdp.evaluate('document.querySelectorAll(".imu-sensor-axes [data-axis]").length === 0')
    async def subscribers(count):
        values = [json.loads((area / (role + "-status.json")).read_text()) for role in ("imu", "peer")]
        return (values[0]["imu_subscribers"] == count and all(value["health_subscribers"] == count and value["shared_subscribers"] == 0 for value in values))

    await wait_for(lambda: cdp.evaluate('!!document.querySelector(".imu-plot")'))
    await cdp.evaluate('document.querySelector(\'tr[data-unit="peer"] button[data-command="start"]\').click(); true')
    await wait_for(lambda: cdp.evaluate('document.querySelector(\'tr[data-unit="peer"] td:nth-child(2)\').textContent.includes("running")'))
    await wait_for(lambda: subscribers(0))
    first_run = (await unit("idle"))["run_id"]
    await cdp.evaluate('document.getElementById("ros-observe").click(); true')
    await wait_for(fresh)
    await wait_for(lambda: state("imu-health", "progressing"))
    await wait_for(lambda: state("peer-health", "progressing"))
    axes = await cdp.evaluate('[...document.querySelectorAll(".imu-sensor-axes [data-axis]")].map(axis=>({...axis.dataset}))')
    assert len(axes) == 3 and float(axes[0]["screenX"]) > 210 > float(axes[1]["screenX"])
    assert float(axes[2]["screenY"]) < 160
    value = await topic()
    assert value["data"]["timestamp_semantics"] == "estimated_mcu_callback_receipt" and value["acquisition_age_seconds"] is None
    assert isinstance(value["data"]["sample_timestamp_ns"], str) and int(value["data"]["sample_timestamp_ns"]) > 2**53
    assert await cdp.evaluate('document.querySelector(".imu-card").textContent.includes("estimated MCU receipt") && !document.querySelector(".imu-card").textContent.includes("acquired")')
    check("native_imu_preserves_receipt_estimate_quaternion_vectors_and_sensor_axes", sample=value, axes=axes)

    control(yaw=90)
    await wait_for(lambda: cdp.evaluate('Number(document.querySelector(\'.imu-sensor-axes [data-axis="x"]\')?.dataset.screenX) < 210'))
    control(missing=["orientation", "angular_velocity"])
    await wait_for(lambda: state("imu", "partial"))
    assert await no_pose()
    value = await topic()
    assert value["data"]["orientation"] is None and value["data"]["angular_velocity"] is None
    assert value["data"]["linear_acceleration"]["value"]["z"] == 9.81
    assert await cdp.evaluate('document.querySelector(".imu-values dd").textContent === "Unavailable in this sample"')
    control(level=1, state="CALIBRATING")
    await wait_for(fresh)
    await wait_for(lambda: state("imu-health", "warning"))
    assert await state("peer-health", "progressing")
    check("quaternion_rotation_partial_fields_and_calibration_warning_keep_distinct_meanings")

    for settings, expected in ((dict(frame="wrong"), "frame_mismatch"), (dict(stamp_offset=10), "timestamp_in_future"),
                               (dict(invalid_quaternion=True), "invalid_quaternion"), (dict(frame="x" * 4000), "sample_too_large"),
                               (dict(missing=["orientation", "angular_velocity", "linear_acceleration"]), "measurement_unavailable")):
        control(**settings)
        await wait_for(lambda: sample_status(expected))
        await wait_for(no_pose)
    control(freeze_stamp=True)
    await wait_for(lambda: sample_status("stale_source"))
    await wait_for(no_pose)
    value = await topic()
    assert value["age_seconds"] < .1 and value["sample_age_seconds"] >= .1 and not value["fresh"]
    control()
    await wait_for(fresh)
    control(publish=False)
    await wait_for(no_pose)
    assert await state("peer-health", "progressing")
    check("invalid_and_stale_imu_samples_clear_pose_without_disturbing_other_provider", stale_sample=value)

    control()
    await wait_for(fresh)
    control("peer", collide=True)
    async def ambiguous():
        return (await topic("imu-health"))["status"] == "source_ambiguous"
    await wait_for(ambiguous)
    await wait_for(lambda: state("imu-health", "unavailable"))
    assert await state("peer-health", "progressing")
    control("peer")
    await wait_for(lambda: state("imu-health", "progressing"))
    await wait_for(lambda: subscribers(1))
    check("separate_declared_diagnostic_topics_ignore_shared_topic_and_reject_private_topic_collisions")

    responses = []
    try:
        for _ in range(5):
            response = await client.get(origin + "/api/ros/events?topic=imu&topic=imu-health&topic=peer-health")
            assert response.status == 200
            responses.append(response)
            await asyncio.wait_for(response.content.readline(), 5)
        await wait_for(lambda: subscribers(1))
        check("five_https_observers_and_browser_share_one_subscription_per_capability")
    finally:
        for response in responses: response.close()
    await cdp.evaluate('document.getElementById("ros-observe").click(); true')
    await wait_for(lambda: subscribers(0), seconds=12)
    assert await no_pose()
    await cdp.evaluate('document.getElementById("ros-observe").click(); true')
    await wait_for(fresh)
    await cdp.evaluate('Object.defineProperty(document,"hidden",{configurable:true,value:true}); document.dispatchEvent(new Event("visibilitychange")); true')
    await wait_for(lambda: subscribers(0), seconds=12)
    assert await no_pose()
    await cdp.evaluate('delete document.hidden; document.dispatchEvent(new Event("visibilitychange")); true')
    await wait_for(fresh)
    check("pause_and_simulated_hidden_page_release_all_sensor_demand_and_clear_pose")

    await cdp.evaluate('document.getElementById("ros-panel").scrollIntoView(); true')
    (area / "browser-imu.png").write_bytes(base64.b64decode((await cdp.call("Page.captureScreenshot", format="png"))["data"]))
    control("peer", exit=True)
    async def peer_failed():
        return (await unit("peer"))["lifecycle"] == "failed"
    await wait_for(peer_failed)
    await wait_for(lambda: state("peer-health", "unavailable"))
    await wait_for(fresh)
    current = await unit("idle")
    assert current["run_id"] == first_run and current["lifecycle"] == "running"
    assert not (await unit("peer"))["blocked"]
    check("other_provider_failure_clears_only_its_health_and_does_not_restart_or_stop_imu", imu_run=first_run)
    control("peer")
    await cdp.evaluate('window.scrollTo(0,0); true')
