"""Three real Chromium tabs, native fixture data, and reusable compositions."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import base64
import json

from integration_browser import CDP, wait_for


async def exercise(cdp, devtools, debug_port, origin, local, area, check):
    targets, connections, pages = [], [], [cdp]
    # Ordinary browser tab suspension is a separate policy. Keep these fixture
    # pages visibly active so all three streams exert simultaneous demand.
    visible = 'Object.defineProperty(document,"hidden",{configurable:true,get:()=>false});'
    observe = ('document.getElementById("resource-panel").open=true; '
               'if(document.getElementById("ros-observe").textContent.includes("Observe")) '
               'document.getElementById("ros-observe").click(); true')
    try:
        for _ in range(2):
            target = (await cdp.call("Target.createTarget", url="about:blank"))["targetId"]
            targets.append(target)
            async with devtools.get(f"http://127.0.0.1:{debug_port}/json/list") as response:
                websocket = next(item["webSocketDebuggerUrl"] for item in await response.json() if item["id"] == target)
            connection = await devtools.ws_connect(websocket)
            connections.append(connection)
            page = CDP(connection)
            await page.call("Runtime.enable")
            await page.call("Page.addScriptToEvaluateOnNewDocument", source=visible)
            await page.call("Page.navigate", url=origin)
            pages.append(page)
        for page in pages:
            await wait_for(lambda page=page: page.evaluate('typeof pageClients !== "undefined" && pageClients && fresh() && !!document.querySelector("[data-ros-topic]")'))
            await page.evaluate(visible + 'document.dispatchEvent(new Event("visibilitychange"));' + observe)
        for page in pages:
            await wait_for(lambda page=page: page.evaluate(
                'pageClients.observations.state.snapshot?.topics.some(t=>t.fresh) && pageClients.resources.state.snapshot?.fresh'))
        before = await local("status")
        results = []
        for page in pages:
            results.append(await page.evaluate("""(async()=>{
              const begin=performance.now(), ids=[];
              for(let i=0;i<5;i++){
                const reply=await pageClients.http.get('api/ros/actions',{timeoutMs:2000});
                ids.push(typeof reply==='object');
              }
              let rejection;
              try { await pageClients.http.post('api/units/missing-fixture/start',{
                request_id:crypto.randomUUID(), expires_at_ms:state.server_time_ms+10000,
                config_revision:state.config_revision
              },{timeoutMs:2000}); } catch(error) { rejection={status:error.status,code:error.code}; }
              return {reads:ids, milliseconds:performance.now()-begin,rejection,
                channels:[pageClients.runtime,pageClients.observations,pageClients.resources].map(c=>c.state.connection),
                physicalMembers:pageClients.runtime.transport.members.size,
                sourceOpen:pageClients.runtime.transport.source.readyState===EventSource.OPEN};
            })()"""))
        assert all(all(item["reads"]) and item["sourceOpen"] and item["channels"] == ["open"] * 3
                   and item["physicalMembers"] == 3 and item["rejection"]["status"] in (400, 404, 409) for item in results), results
        after = await local("status")
        assert [(unit["unit_id"], unit["run_id"]) for unit in before["units"]] == [
            (unit["unit_id"], unit["run_id"]) for unit in after["units"]]
        check("three_tabs_share_one_stream_each_and_http_reads_and_command_rejections_complete",
              results=results, synthetic_visibility=True, independent_devices=False)
        for page, path in zip(pages[1:], ("/autonomous", "/outreach")):
            await page.call("Emulation.setDeviceMetricsOverride", width=1000, height=1000,
                            deviceScaleFactor=1, mobile=False)
            await page.call("Page.navigate", url=origin + path)
            await wait_for(lambda page=page: page.evaluate('!!document.querySelector("[data-runtime-unit=idle]")'))
            assert await page.evaluate('document.querySelector("h1").textContent.includes("monitor")')
            if path == "/outreach":
                await wait_for(lambda page=page: page.evaluate('!document.getElementById("observe").disabled'))
                await page.evaluate('document.getElementById("observe").click(); true')
                await wait_for(lambda page=page: page.evaluate('!!document.querySelector("#sensors .badge.live")'))
            shot = await page.call("Page.captureScreenshot", format="png")
            (area / (path[1:] + ".png")).write_bytes(base64.b64decode(shot["data"]))
            assert not page.exceptions, page.exceptions
        check("autonomous_and_outreach_compositions_reuse_packaged_clients_and_views")
    finally:
        for target in targets:
            await cdp.call("Target.closeTarget", targetId=target)
        for connection in connections:
            await connection.close()
        await cdp.call("Page.bringToFront")
        await cdp.evaluate('document.getElementById("resource-panel").open=false; true')
