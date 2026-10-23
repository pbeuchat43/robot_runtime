"""Native marker poses, metric 3D geometry and independent live layers in Chromium."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import base64
import json
import math
import os

from integration_browser import wait_for


async def exercise(cdp, local, area, check, client, origin):
    def control(**values):
        temporary = area / "markers-control.tmp"
        temporary.write_text(json.dumps(values))
        os.replace(temporary, area / "markers-control.json")
    async def topic():
        return next(item for item in (await local("ros_status"))["topics"] if item["id"] == "fiducials")
    async def state(key, expected):
        return await cdp.evaluate(f'document.querySelector("[data-ros-topic={key}]")?.dataset.observationState === {json.dumps(expected)}')
    async def fresh():
        return all([await state(key,"fresh") for key in ("cones","scan","fiducials")])
    async def subscribers(counts):
        return all(json.loads((area / (role+"-status.json")).read_text())["subscribers"] == count
                   for role,count in zip(("cones","lidar","markers"),counts))
    async def click(label):
        await cdp.evaluate('[...document.querySelectorAll(".spatial-card button")].find(b=>b.textContent==='+json.dumps(label)+').click(); true')
    async def centers():
        return await cdp.evaluate('[...document.querySelectorAll(".fiducial-points [data-marker-id]")].map(p=>[Number(p.dataset.screenX),Number(p.dataset.screenY)])')
    for unit in ("peer","markers"):
        await cdp.evaluate(f'document.querySelector("tr[data-unit={unit}] button[data-command=start]").click(); true')
    await wait_for(lambda: subscribers([0,0,0]))
    await cdp.evaluate('document.getElementById("ros-observe").click(); true')
    await wait_for(fresh)
    before = {unit["unit_id"]:unit["run_id"] for unit in (await local("status",fresh=True))["units"]}
    sample = await topic()
    poses = sample["data"]["detections"]
    assert sample["data"]["dictionary_name"] == "DICT_4X4_50"
    assert len(poses) == 2 and poses[0]["id"] == poses[1]["id"] == 7
    assert math.isclose(poses[0]["marker_size_m"],.081,abs_tol=1e-7)
    assert math.isclose(poses[0]["position"]["z"],.4,abs_tol=1e-7)
    assert math.isclose(poses[1]["position"]["z"],.8,abs_tol=1e-7)
    assert await cdp.evaluate('document.querySelector(".spatial-card").dataset.dimension === "2d" && document.querySelectorAll(".spatial-plot").length === 1')
    flat = await centers()
    assert flat[0] == flat[1]
    check("native_fiducial_metadata_duplicate_ids_and_full_camera_to_base_transform", sample=sample)
    await click("3D")
    spatial = await centers()
    assert spatial[0] != spatial[1]
    geometry = await cdp.evaluate('[...document.querySelectorAll(".fiducial-points polygon")].map(p=>({size:Number(p.dataset.sizeM),corners:JSON.parse(p.dataset.corners),points:p.getAttribute("points")}))')
    assert len(geometry) == 2
    for marker in geometry:
        for a,b in zip(marker["corners"],marker["corners"][1:]+marker["corners"][:1]):
            assert math.isclose(math.dist(a,b),marker["size"],rel_tol=1e-6)
    # The first marker is vertical in base_link: its normal points along +x.
    assert all(math.isclose(point[0],1.7,abs_tol=1e-7) for point in geometry[0]["corners"])
    assert len({round(point[0],6) for point in geometry[1]["corners"]}) > 1
    await click("Top")
    top = await centers()
    assert math.dist(top[0],top[1]) < 1e-6
    await click("Front")
    front = await centers()
    assert math.isclose(front[0][0],front[1][0]) and front[0][1] > front[1][1]
    await click("Reset view")
    assert await centers() == spatial
    await click("Zoom in")
    zoomed = await centers()
    assert zoomed != spatial
    # Keyboard and real pointer input both change the view without changing demand.
    await cdp.evaluate('document.querySelector(".spatial-plot").focus(); true')
    await cdp.call("Input.dispatchKeyEvent", type="keyDown", key="ArrowLeft", code="ArrowLeft", windowsVirtualKeyCode=37)
    assert await centers() != zoomed
    await click("Pan")
    await cdp.evaluate('document.querySelector(".spatial-card").scrollIntoView(); true')
    rect = await cdp.evaluate('(()=>{const r=document.querySelector(".spatial-plot").getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()')
    pan_before = await centers()
    await cdp.call("Input.dispatchMouseEvent",type="mousePressed",button="left",clickCount=1,**rect)
    await cdp.call("Input.dispatchMouseEvent",type="mouseMoved",button="left",buttons=1,x=rect["x"]+20,y=rect["y"]+10)
    await cdp.call("Input.dispatchMouseEvent",type="mouseReleased",button="left",clickCount=1,x=rect["x"]+20,y=rect["y"]+10)
    assert await centers() != pan_before
    await click("Pan")
    await click("Reset view")
    await wait_for(lambda:subscribers([1,1,1]))
    assert before == {unit["unit_id"]:unit["run_id"] for unit in (await local("status",fresh=True))["units"]}
    check("true_size_oriented_marker_planes_height_orbit_pan_zoom_presets_and_reset_preserve_runs", geometry=geometry)
    for width in (1400,900,390):
        await cdp.call("Emulation.setDeviceMetricsOverride",width=width,height=1000,deviceScaleFactor=1,mobile=False)
        await cdp.evaluate('document.querySelector(".spatial-card").scrollIntoView(); true')
        assert await cdp.evaluate('document.querySelector(".spatial-card").getBoundingClientRect().right <= innerWidth && document.documentElement.scrollWidth <= innerWidth')
        (area / f"browser-fiducials-3d-{width}.png").write_bytes(base64.b64decode((await cdp.call("Page.captureScreenshot",format="png"))["data"]))
    await cdp.call("Emulation.setDeviceMetricsOverride",width=1400,height=1000,deviceScaleFactor=1,mobile=False)
    await click("2D")
    assert await centers() == flat
    (area / "browser-fiducials-2d.png").write_bytes(base64.b64decode((await cdp.call("Page.captureScreenshot",format="png"))["data"]))
    for key, counts in (("fiducials",[1,1,0]),("scan",[1,0,1]),("cones",[0,1,1])):
        await cdp.evaluate(f'document.querySelector("[data-ros-topic={key}] input").click(); true')
        await wait_for(lambda:subscribers(counts),seconds=12)
        assert await state(key,"unavailable")
        await cdp.evaluate(f'document.querySelector("[data-ros-topic={key}] input").click(); true')
        await wait_for(fresh)
    control(invalid_pose=True)
    async def invalid(): return (await topic()).get("data",{}).get("invalid_pose_count") == 2
    await wait_for(invalid)
    await wait_for(lambda:cdp.evaluate('document.querySelectorAll(".fiducial-points [data-marker-id]").length === 0'))
    assert await state("fiducials","fresh") and await state("cones","fresh") and await state("scan","fresh")
    control(count=0)
    await wait_for(lambda:state("fiducials","empty"))
    control(count=25)
    async def truncated(): return (await topic()).get("data",{}).get("truncated") is True
    await wait_for(truncated)
    for settings, expected in ((dict(frame="map"),"frame_mismatch"),(dict(bad_quaternion=True),"invalid_quaternion"),
        (dict(stamp_offset=10),"timestamp_in_future"),(dict(freeze_stamp=True),"stale_source"),
        (dict(publish=False),"stale"),(dict(collide=True),"source_ambiguous")):
        control(**settings)
        async def status(): return (await topic())["status"] == expected
        await wait_for(status)
        await wait_for(lambda:state("fiducials","unavailable"))
        assert await state("cones","fresh") and await state("scan","fresh")
        control()
        await wait_for(fresh)
    check("independent_demand_invalid_pose_empty_truncation_staleness_and_collision_clear_only_markers")
    await cdp.evaluate('Object.defineProperty(document,"hidden",{configurable:true,value:true}); document.dispatchEvent(new Event("visibilitychange")); true')
    await wait_for(lambda:subscribers([0,0,0]),seconds=12)
    assert await cdp.evaluate('document.querySelectorAll(".fiducial-points,.lidar-points,.cone-points").length === 0')
    await cdp.evaluate('delete document.hidden; document.dispatchEvent(new Event("visibilitychange")); true')
    await wait_for(fresh)
    control(exit=True)
    await wait_for(lambda:state("fiducials","unavailable"))
    assert await state("cones","fresh") and await state("scan","fresh")
    check("hidden_page_releases_all_layers_and_marker_provider_failure_preserves_siblings")
    control()
    await cdp.evaluate('window.scrollTo(0,0); true')
