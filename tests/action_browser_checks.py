"""Real Chromium actions, public observation and no replay on reconnect."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import asyncio
import base64
import json
import time
import uuid

from integration_browser import wait_for


async def exercise(cdp, api, local, area, check, command_headers, origin, passphrase):
    selector = '[data-ros-action="count"]'
    async def action():
        return (await local("ros_actions"))["actions"][0]
    async def accepted():
        value = await action()
        return value if value["goal"] and value["goal"].get("accepted") else None
    async def terminal():
        value = await action()
        return value if value["goal"] and value["goal"].get("terminal") else None
    def provider():
        return json.loads((area / "provider-status.json").read_text())
    def control(**values):
        temporary = area / "control.tmp"
        temporary.write_text(json.dumps(values))
        temporary.replace(area / "control.json")
    async def submit(steps=60):
        await wait_for(lambda: cdp.evaluate(f'!!document.querySelector({json.dumps(selector)}) && !document.querySelector({json.dumps(selector + " button[type=submit]")}).disabled'))
        await cdp.evaluate(f'document.querySelector({json.dumps(selector + " [name=steps]")}).value={steps}; document.querySelector({json.dumps(selector + " [name=period_seconds]")}).value=.1; document.querySelector({json.dumps(selector + " button[type=submit]")}).click(); true')

    # Simulate an acceptance response lost after the request reached the server.
    await cdp.evaluate('window.actionFetch=window.fetch; window.fetch=async (...args)=>{ const response=await window.actionFetch(...args); if(String(args[0]).endsWith("/goals")) throw new TypeError("Synthetic lost response"); return response; }; true')
    await submit(100)
    first = await wait_for(accepted)
    goal_id = first["goal"]["request_id"]
    await wait_for(lambda: cdp.evaluate('document.querySelector(".action-feedback").dataset.fresh === "true"'))
    before = provider()["goals_received"]
    await cdp.call("Page.reload")
    await wait_for(lambda: cdp.evaluate('!!document.querySelector(".action-state") && document.querySelector(".action-state").textContent.includes(' + json.dumps(goal_id) + ')'))
    await cdp.evaluate(f'document.querySelector({json.dumps(selector + " [name=steps]")}).value=37; true')
    await asyncio.sleep(1.3)
    assert await cdp.evaluate(f'document.querySelector({json.dumps(selector + " [name=steps]")}).value === "37"')
    assert provider()["goals_received"] == before
    check("lost_goal_reply_and_reload_reconcile_by_id_without_replay_and_preserve_drafts")
    control(pause_feedback=True)
    await wait_for(lambda: cdp.evaluate('document.querySelector(".action-feedback").dataset.fresh === "false"'))
    assert not (await action())["goal"]["terminal"]
    control(cancel_delay=2)
    value = await local("ros_actions")
    row = value["actions"][0]
    payload = dict(request_id=str(uuid.uuid4()), goal_request_id=goal_id, config_revision=value["config_revision"],
        generation=value["generation"], expected_run=row["run_id"], expires_at_ms=int(time.time()*1000)+10000)
    code, _value, _headers = await api("/api/ros/actions/count/cancel", "POST", payload, command_headers)
    assert code == 202
    await wait_for(lambda: cdp.evaluate('document.querySelector(".action-cancellation").textContent.includes("cancel accepted")'))
    assert not (await action())["goal"]["terminal"]
    await wait_for(terminal)
    assert (await action())["goal"]["goal_status"] == "canceled"
    await wait_for(lambda: cdp.evaluate('document.querySelector(".action-result").textContent.includes("Counting canceled")'))
    check("feedback_expires_and_another_unlocked_session_can_cancel_with_separate_ack_and_result")
    control()
    await submit(50)
    second = await wait_for(lambda: newer_goal(action, goal_id))
    goal_id = second["goal"]["request_id"]
    before = provider()["goals_received"]
    await cdp.call("Page.navigate", url="about:blank")
    await asyncio.sleep(1)
    assert provider()["cancel_requests"] == 1
    await wait_for(terminal)
    assert (await action())["goal"]["goal_status"] == "succeeded"
    await cdp.call("Page.navigate", url=origin)
    await wait_for(lambda: cdp.evaluate('!!document.querySelector(".action-result") && document.querySelector(".action-result").textContent.includes("Counting complete")'))
    assert provider()["goals_received"] == before
    check("browser_departure_keeps_goal_running_and_return_observes_completion_without_resubmit")
    await cdp.evaluate('document.getElementById("lock").click(); true')
    await wait_for(lambda: cdp.evaluate('!document.getElementById("unlock").hidden'))
    assert await cdp.evaluate(f'document.querySelector({json.dumps(selector + " button[type=submit]")}).disabled')
    code, value, _headers = await api("/api/ros/actions")
    assert code == 200 and value["actions"][0]["goal"]["request_id"] == goal_id
    for width in (1400, 900):
        await cdp.call("Emulation.setDeviceMetricsOverride", width=width, height=1100, deviceScaleFactor=1, mobile=False)
        await cdp.evaluate('document.getElementById("action-panel").scrollIntoView(); true')
        layout = await cdp.evaluate('({display:getComputedStyle(document.querySelector(".ros-action")).display,overflow:document.documentElement.scrollWidth>innerWidth,inputs:[...document.querySelectorAll(".ros-action input[type=number]")].every(i=>i.getBoundingClientRect().width>100)})')
        assert layout == dict(display="block", overflow=False, inputs=True), layout
        shot = await cdp.call("Page.captureScreenshot", format="png")
        (area / f"actions-{width}.png").write_bytes(base64.b64decode(shot["data"]))
    check("action_form_layout_is_readable_at_1400_and_900_pixels")
    check("locked_browser_can_observe_results_but_cannot_send_or_cancel")
    await cdp.evaluate('document.getElementById("passphrase").value=' + json.dumps(passphrase) + '; document.querySelector("#unlock button").click(); true')
    await wait_for(lambda: cdp.evaluate('document.getElementById("unlock").hidden'))


async def newer_goal(action, previous):
    value = await action()
    return value if value["goal"] and value["goal"]["request_id"] != previous and value["goal"].get("accepted") else None
