"""ROS extension to the real Chromium harness; synthetic providers only."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import asyncio
import base64
import json
import os
import time
import uuid

from integration_browser import wait_for


async def exercise(cdp, api, local, area, check, command_headers, client, origin):
    def provider():
        return json.loads((area / "provider-status.json").read_text())

    async def subscribers(count):
        return provider()["counter_subscribers"] == count

    async def catalog():
        return await local("ros_status")

    async def service_ready():
        return (await catalog())["services"][0]["available"]

    async def sample():
        return (await catalog())["topics"][0]

    async def observed():
        return await cdp.evaluate('document.querySelector("[data-ros-topic=counter] .badge")?.textContent === "Fresh sample"')

    async def request_finished(text):
        return await cdp.evaluate('document.querySelector("[data-ros-service=enable] .ros-result").textContent.includes(' + json.dumps(text) + ')')

    async def button_ready():
        return await cdp.evaluate('!!document.querySelector("[data-ros-service=enable] button") && !document.querySelector("[data-ros-service=enable] button").disabled')

    def control(**values):
        temporary = area / "control.tmp"
        temporary.write_text(json.dumps(values))
        os.replace(temporary, area / "control.json")

    await wait_for(service_ready)
    await wait_for(lambda: cdp.evaluate('!!document.querySelector("[data-ros-topic=counter]")'))
    assert provider()["counter_subscribers"] == 0
    check("ros_browser_initial_view_has_no_topic_demand")
    await cdp.evaluate('document.getElementById("ros-observe").click(); true')
    await wait_for(observed)
    value = await cdp.evaluate('JSON.parse(document.querySelector("[data-ros-topic=counter] pre").textContent).data')
    assert isinstance(value, str) and int(value) > 2**53
    await wait_for(lambda: subscribers(1))
    generation = (await sample())["stream_generation"]
    check("ros_browser_renders_fresh_lossless_int64", displayed=value)
    control(publish=False)
    await wait_for(lambda: cdp.evaluate('document.querySelector("[data-ros-topic=counter] .badge").textContent.toLowerCase().includes("stale")'))
    control()
    await wait_for(observed)
    await cdp.evaluate('document.getElementById("ros-observe").click(); true')
    await wait_for(lambda: subscribers(0), seconds=12)
    await cdp.evaluate('document.getElementById("ros-observe").click(); true')
    await wait_for(observed)
    assert (await sample())["stream_generation"] != generation
    check("ros_browser_marks_stale_and_pause_releases_then_resumes_new_stream")

    # Exercise the page-visibility event handler deterministically. This is a
    # simulated hidden state in Chromium, not an OS/window-manager measurement.
    await cdp.evaluate('Object.defineProperty(document,"hidden",{configurable:true,value:true}); document.dispatchEvent(new Event("visibilitychange")); true')
    await wait_for(lambda: subscribers(0), seconds=12)
    await cdp.evaluate('delete document.hidden; document.dispatchEvent(new Event("visibilitychange")); true')
    await wait_for(observed)
    check("ros_browser_simulated_visibility_handler_releases_and_resumes")

    await wait_for(button_ready)
    await cdp.evaluate('document.querySelector("[data-ros-service=enable] button").click(); true')
    await wait_for(lambda: request_finished("provider accepted"))
    await wait_for(button_ready)
    await cdp.evaluate('document.querySelector("[data-ros-service=enable] input[name=data]").checked=false; document.querySelector("[data-ros-service=enable] button").click(); true')
    await wait_for(lambda: request_finished("provider rejected"))
    check("ros_browser_typed_service_acceptance_and_refusal")

    status = await catalog()
    body = dict(request_id=str(uuid.uuid4()), config_revision=status["config_revision"],
                expected_run=status["services"][0]["run_id"], generation=status["generation"],
                expires_at_ms=int(time.time() * 1000) + 10000, arguments=dict(data=True))
    for path, headers in (("/api/ros/services/enable", {}), ("/api/ros/services/arbitrary", command_headers)):
        code, _, _ = await api(path, "POST", body, headers)
        assert code == (403 if not headers else 404)
    check("ros_https_requires_csrf_and_declared_capability")

    await wait_for(button_ready)
    control(delay=1.6)
    before = provider()["calls"]
    await cdp.evaluate('document.querySelector("[data-ros-service=enable] input[name=data]").checked=true; document.querySelector("[data-ros-service=enable] button").click(); true')
    await wait_for(lambda: request_finished("timed out"))
    async def blocked():
        return (await catalog())["services"][0]["blocked_by"]
    uncertain = await wait_for(blocked)
    assert await cdp.evaluate('document.querySelector("[data-ros-service=enable] button").disabled')
    await cdp.call("Page.reload")
    await wait_for(lambda: cdp.evaluate('!!document.querySelector("[data-ros-service=enable] button")'))
    await asyncio.sleep(1)
    assert provider()["calls"] == before + 1
    assert await cdp.evaluate('document.querySelector("[data-ros-service=enable] button").disabled')
    control()
    await local("ros_acknowledge", request_id=uncertain, expected_run=status["services"][0]["run_id"])
    check("ros_browser_timeout_and_reload_never_replay_and_require_local_acknowledgement")

    responses = []
    try:
        for _ in range(5):
            response = await client.get(origin + "/api/ros/events?topic=counter")
            assert response.status == 200
            responses.append(response)
            assert (await asyncio.wait_for(response.content.readline(), 5)).startswith(b"data: ")
        await wait_for(lambda: subscribers(1))
        assert (await catalog())["subscriptions"] == ["counter"]
        check("five_https_ros_streams_share_one_native_subscription")
    finally:
        for response in responses:
            response.close()
    await wait_for(lambda: subscribers(0), seconds=12)
    check("last_https_ros_consumer_releases_native_subscription")
    await wait_for(button_ready)
    check("local_uncertainty_acknowledgement_updates_open_browser")
    await cdp.evaluate('document.getElementById("ros-observe").click(); document.getElementById("ros-panel").scrollIntoView(); true')
    await wait_for(observed)
    shot = await cdp.call("Page.captureScreenshot", format="png")
    (area / "browser-ros.png").write_bytes(base64.b64decode(shot["data"]))
    await cdp.evaluate('window.scrollTo(0,0); true')
