"""Installed UI and HTTPS parameter workflow with independent clients."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import asyncio
import base64
import json
import os
import time
import uuid

import aiohttp

from integration_browser import wait_for


async def exercise(cdp, api, local, area, check, headers, client, origin):
    group = '[data-parameter-group="settings"]'
    def control(**values):
        path = area / "control.tmp"; path.write_text(json.dumps(values)); os.replace(path,area/"control.json")
    def provider():
        return json.loads((area/"provider-status.json").read_text())
    async def js(value):
        return await cdp.evaluate(value)
    async def read():
        await asyncio.sleep(1.05)
        await wait_for(lambda: js(f'!document.querySelector(\'{group} button.secondary\').disabled'))
        await js(f'document.querySelector(\'{group} button.secondary\').click(); true')
        await wait_for(lambda: js(f'document.querySelector(\'{group} .ros-result\').textContent.includes("Values read.")'))
    async def draft(name, value):
        selector = group + f' [data-parameter="{name}"]'
        await js(f'{{const field=document.querySelector({json.dumps(selector)}); field.querySelector(".parameter-name input").click(); field.querySelector(".parameter-value input, .parameter-value textarea").value={json.dumps(value)};}} true')
    async def apply():
        await wait_for(lambda: js(f'!document.querySelector(\'{group} button[type=submit]\').disabled'))
        await js(f'document.querySelector(\'{group} button[type=submit]\').click(); true')
    async def result(text):
        return await wait_for(lambda: js(f'document.querySelector(\'{group} .ros-result\').textContent.includes({json.dumps(text)})'))
    await wait_for(lambda: js(f'!!document.querySelector(\'{group}\')'))
    await read()
    assert await js('document.querySelector(\'[data-parameter="identity"] .parameter-name input\').disabled')
    assert await js('document.querySelector(\'[data-parameter="private"] .parameter-name input\').disabled')
    assert await js('document.querySelector(\'[data-parameter="count"] .parameter-observed\').textContent.includes("9007199254740993")')
    assert await js('document.querySelector(\'[data-parameter="rate"]\').textContent.includes("Range 1 to 20")')
    await draft("title", "browser edit"); await draft("count", "9223372036854775807"); await apply(); await result("matches requested values")
    assert provider()["title"] == "browser edit" and provider()["count"] == "9223372036854775807"
    check("browser_typed_drafts_ranges_readonly_atomic_edit_and_lossless_readback")
    await read(); await draft("title", "rejected draft"); await draft("rate", "13"); await apply(); await result("provider rejected")
    assert provider()["title"] == "browser edit"
    check("browser_displays_provider_rejection_without_partial_atomic_change")
    await read(); await draft("title", "browser draft kept")
    cached_code, snapshot, _ = await api("/api/ros/parameters/settings")
    assert cached_code == 200 and snapshot["fresh"]
    command = dict(request_id=str(uuid.uuid4()), config_revision=snapshot["config_revision"], expected_run=snapshot["run_id"],
        expected_snapshot=snapshot["snapshot_id"], generation=snapshot["generation"], expires_at_ms=int(time.time()*1000)+10000,
        changes=dict(title=dict(type="string",value="second client")))
    code, _, _ = await api("/api/ros/parameters/settings","POST",command,{})
    assert code == 403
    code, operation, _ = await api("/api/ros/parameters/settings","POST",command,headers)
    assert code == 202, operation
    async def done():
        value = await local("operation",request_id=command["request_id"])
        return value if value.get("readback",{}).get("status") == "observed" else None
    await wait_for(done)
    await wait_for(lambda: js(f'document.querySelector(\'{group} button[type=submit]\').disabled'))
    assert await js('document.querySelector(\'[data-parameter="title"] .parameter-value input\').value === "browser draft kept"')
    code, _, _ = await api("/api/ros/parameters/settings","POST",{**command,"request_id":str(uuid.uuid4())},headers)
    assert code != 202
    check("independent_client_invalidates_browser_baseline_without_rebasing_draft_and_csrf_is_enforced")
    await read(); control(mismatch=True); await draft("title","request with mismatch"); await apply(); await result("DIFFERS from requested values")
    check("browser_separates_provider_acceptance_from_readback_mismatch")
    control(); await read(); control(readback_delay=4); await draft("title","lost readback"); await apply(); await result("Readback: unavailable")
    assert await js(f'document.querySelector(\'{group} .ros-result\').textContent.includes("provider accepted")')
    check("browser_keeps_acceptance_visible_when_readback_fails")
    await asyncio.sleep(1.5); control(); await read(); control(set_delay=4)
    sets = provider()["counts"]["set"]
    await draft("title","uncertain browser edit"); await apply()
    async def received(): return provider()["counts"]["set"] == sets+1
    await wait_for(received)
    await cdp.call("Page.reload")
    await wait_for(lambda: js(f'!!document.querySelector(\'{group}\')'))
    await result("response deadline exceeded")
    await asyncio.sleep(1.5); control()
    assert provider()["counts"]["set"] == sets+1
    assert await js(f'document.querySelector(\'{group} button[type=submit]\').disabled')
    check("browser_reload_reconciles_uncertain_request_without_replay")
    await read()
    await js('document.getElementById("parameter-panel").scrollIntoView(); true')
    shot = await cdp.call("Page.captureScreenshot",format="png",captureBeyondViewport=True)
    (area/"browser-parameters.png").write_bytes(base64.b64decode(shot["data"]))
    async with aiohttp.ClientSession(connector=client.connector, connector_owner=False) as public:
        async with public.get(origin + "/api/ros/parameters/settings") as response: assert response.status == 200
        async with public.post(origin + "/api/ros/parameters/settings", json=command, headers={"Origin":origin}) as response: assert response.status == 401
        async with public.get(origin + "/api/operations/" + command["request_id"]) as response: assert response.status == 401
    check("public_parameter_reads_cannot_write_or_access_mutation_results")
