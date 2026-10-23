"""Browser fixed choices, loss/reload without replay and expiring observations."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import asyncio
import base64
import json

from integration_browser import wait_for


async def exercise(cdp, local, area, check, passphrase):
    select = 'document.querySelector(\'[data-ros-command="request"] select\')'
    button = 'document.querySelector(\'[data-ros-command="request"] button\')'
    result = 'document.querySelector(\'[data-ros-command="request"] .ros-result\')'
    def provider(): return json.loads((area / "provider-status.json").read_text())
    await wait_for(lambda: cdp.evaluate(f'!!{button} && !{button}.disabled'))
    assert await cdp.evaluate(f'[...{select}.options].map(o=>o.value)') == ["", "disable", "enable", "disarm"]
    await cdp.evaluate('document.getElementById("ros-observe").click(); true')
    await wait_for(lambda: cdp.evaluate('document.querySelector(\'[data-ros-topic="state"] .badge\').textContent === "Fresh sample"'))
    # Hold operation reads until reload so its saved ID is definitely pending.
    await cdp.evaluate('window.originalFetch=window.fetch; window.fetch=async (...args)=>{ if(String(args[0]).includes("/api/operations/")) throw new TypeError("Synthetic lost reply"); const response=await window.originalFetch(...args); if(String(args[0]).includes("/api/ros/commands/")) throw new TypeError("Synthetic lost reply"); return response; }; true')
    await cdp.evaluate(f'{select}.value="enable"; {button}.click(); true')
    async def received(): return provider()["received"] == [1]
    await wait_for(received)
    assert await cdp.evaluate('Object.keys(JSON.parse(sessionStorage.getItem("runtime-ros-pending"))).length') == 1
    await cdp.call("Page.reload")
    await wait_for(lambda: cdp.evaluate(f'!!{result} && {result}.textContent.includes("unconfirmed")'))
    await asyncio.sleep(.5)
    assert provider()["received"] == [1]
    check("fixed_choice_lost_reply_and_reload_reconcile_without_replay")
    await cdp.evaluate('document.getElementById("ros-observe").click(); true')
    await wait_for(lambda: cdp.evaluate('document.querySelector(\'[data-ros-topic="state"] .badge\').textContent === "Fresh sample"'))
    (area / "control.json").write_text(json.dumps(dict(pause=True)))
    await wait_for(lambda: cdp.evaluate('document.querySelector(\'[data-ros-topic="state"] .badge\').textContent.toLowerCase().includes("stale")'))
    (area / "control.json").write_text('{}')
    await cdp.evaluate('document.getElementById("lock").click(); true')
    await wait_for(lambda: cdp.evaluate(f'{button}.disabled && !document.getElementById("unlock").hidden'))
    check("observation_expiry_and_locked_command_controls")
    await cdp.call("Emulation.setDeviceMetricsOverride", width=900, height=1000, deviceScaleFactor=1, mobile=False)
    await cdp.evaluate('document.getElementById("ros-services").scrollIntoView(); true')
    assert not await cdp.evaluate('document.documentElement.scrollWidth>innerWidth')
    shot = await cdp.call("Page.captureScreenshot", format="png")
    (area / "commands-900.png").write_bytes(base64.b64decode(shot["data"]))
    await cdp.evaluate('document.getElementById("passphrase").value=' + json.dumps(passphrase) + '; document.querySelector("#unlock button").click(); true')
    await wait_for(lambda: cdp.evaluate('document.getElementById("unlock").hidden'))
