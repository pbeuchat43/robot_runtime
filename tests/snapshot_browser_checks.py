"""Native image helper and actual Chromium checks against synthetic image data."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import asyncio
import base64
import json
import os
import ssl
import time

import aiohttp

from robot_runtime.common import Fault
from integration_browser import wait_for


async def exercise(cdp, local, area, check, origin):
    def control(**values):
        temporary = area / "control.tmp"
        temporary.write_text(json.dumps(values))
        os.replace(temporary, area / "control.json")
    def provider():
        return json.loads((area / "provider-status.json").read_text())
    async def subscribers(count):
        return provider()["image_subscribers"] == count
    async def image_loaded():
        return await cdp.evaluate('(()=>{const i=document.querySelector(".snapshot-card img"); return i && !i.hidden && i.complete && i.naturalWidth>0;})()')
    async def binding():
        value = await local("status", fresh=True)
        return dict(expected_run=value["units"][0]["run_id"], generation=value["generation"], config_revision=value["config_revision"])
    await wait_for(lambda: cdp.evaluate('!!document.querySelector(".snapshot-card")'))
    assert await cdp.evaluate('document.querySelector(".snapshot-card button").disabled')
    assert not provider()["images_enabled"]
    try:
        await local("ros_snapshot", capability_id="image", **await binding())
        raise AssertionError("ordinary run unexpectedly captured an image")
    except Fault as exc:
        assert exc.code == "snapshot_unavailable"
    await cdp.evaluate('document.querySelector("button[data-command=stop]").click(); true')
    await wait_for(lambda: cdp.evaluate('!document.querySelector("button[data-mode=images]").disabled'))
    await cdp.evaluate('document.querySelector("button[data-mode=images]").click(); true')
    await wait_for(lambda: cdp.evaluate('!document.querySelector(".snapshot-card button").disabled'))
    async def enabled():
        return provider()["images_enabled"]
    await wait_for(enabled)
    assert (await local("status"))["units"][0]["launch_mode"]["id"] == "images"
    assert await subscribers(0)
    check("ordinary_start_keeps_images_off_and_explicit_mode_starts_same_unit")
    await cdp.evaluate('document.getElementById("ros-observe").click(); true')
    async def fresh_counter():
        value = (await local("ros_status"))["topics"][0]
        return value if value["fresh"] else None
    await wait_for(fresh_counter)

    # No credential or CSRF token: image viewing remains public at the exact origin.
    context = ssl.create_default_context(cafile=str(area / "cert.pem"))
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=context)) as client:
        async def capture(values=None):
            async with client.post(origin + "/api/ros/snapshots/image", json=values or await binding(), headers={"Origin": origin}) as response:
                return response.status, await response.json()
        control(publish=False)
        requests = [asyncio.create_task(capture()) for _ in range(5)]
        await wait_for(lambda: subscribers(1))
        before = await fresh_counter()
        start = time.monotonic()
        assert (await asyncio.wait_for(local("status", fresh=True), 2))["fresh"]
        status_elapsed = time.monotonic() - start
        await asyncio.sleep(0.3)
        after = await fresh_counter()
        assert before and after and int(after["data"]["data"]) > int(before["data"]["data"])
        assert provider()["maximum_subscribers"] == 1
        control()
        values = await asyncio.gather(*requests)
        assert all(code == 200 for code, _ in values), values
        captures = [value for _, value in values]
        assert len({value["snapshot_id"] for value in captures}) == 1
        sample = captures[0]
        assert sample["acquisition_timestamp_ns"].isdigit() and int(sample["acquisition_timestamp_ns"]) > 2**53
        assert sample["width"] == 640 and sample["height"] == 480 and sample["jpeg_bytes"] <= 131072
        assert base64.b64decode(sample["image_base64"]).startswith(b"\xff\xd8")
        await wait_for(lambda: subscribers(0))
        check("five_public_requests_share_one_capture_and_release_subscription", snapshot={k:v for k,v in sample.items() if k != "image_base64"},
              status_read_during_capture_seconds=status_elapsed, ordinary_topic_progress_during_capture=True)

        for settings, expected in ((dict(stamp_offset=-10), "stale_snapshot"), (dict(stamp_offset=10), "stale_snapshot"),
                                   (dict(frame="wrong"), "invalid_image"), (dict(oversize=True), "image_too_large")):
            await asyncio.sleep(1.1)
            control(**settings)
            code, value = await capture()
            assert code != 200 and value["error"]["code"] == expected, value
            assert "image_base64" not in value
            await wait_for(lambda: subscribers(0))
        control()
        check("old_future_wrong_frame_and_oversized_images_are_not_delivered")

        await asyncio.sleep(1.1)
        bad = dict(await binding(), expected_run="previous")
        code, value = await capture(bad)
        assert code == 409 and value["error"]["code"] == "stale_run"
        async with client.post(origin + "/api/units/idle/start", json=dict(mode="images"), headers={"Origin": origin}) as response:
            assert response.status == 401
        check("stale_capture_targets_and_unauthorized_image_mode_starts_are_rejected")

    await cdp.evaluate('document.querySelector(".snapshot-card button").click(); true')
    try:
        await wait_for(image_loaded)
    except AssertionError as exc:
        note = await cdp.evaluate('document.querySelector(".snapshot-card").textContent')
        raise AssertionError(dict(snapshot_card=note, ros=(await local("status"))["ros"])) from exc
    assert await cdp.evaluate('document.querySelector(".snapshot-card img").naturalWidth') == 640
    await wait_for(lambda: cdp.evaluate('document.querySelector(".snapshot-card .badge").textContent==="Stale snapshot"'))
    assert await cdp.evaluate('document.querySelector(".snapshot-card img").classList.contains("snapshot-stale")')
    check("browser_renders_one_image_then_marks_its_acquisition_stale")

    control(publish=False)
    await cdp.evaluate('document.querySelector(".snapshot-card button").click(); true')
    await wait_for(lambda: subscribers(1))
    await cdp.evaluate('Object.defineProperty(document,"hidden",{configurable:true,value:true}); document.dispatchEvent(new Event("visibilitychange")); true')
    await wait_for(lambda: subscribers(0))
    async def capture_released():
        return (await local("status"))["ros"]["snapshot_capture"] is None
    await wait_for(capture_released)
    assert await cdp.evaluate('document.querySelector(".snapshot-card img").hidden')
    await cdp.evaluate('delete document.hidden; document.dispatchEvent(new Event("visibilitychange")); true')
    control()
    await asyncio.sleep(1.1)
    check("simulated_hidden_page_cancels_pending_capture_and_releases_native_subscription")
    await cdp.evaluate('document.querySelector(".snapshot-card button").click(); true')
    try:
        await wait_for(image_loaded)
    except AssertionError as exc:
        note = await cdp.evaluate('document.querySelector(".snapshot-card").textContent')
        raise AssertionError(dict(snapshot_card=note, ros=(await local("status"))["ros"])) from exc
    await cdp.evaluate('document.getElementById("snapshot-panel").scrollIntoView(); true')
    (area / "browser-snapshot.png").write_bytes(base64.b64decode((await cdp.call("Page.captureScreenshot", format="png"))["data"]))
