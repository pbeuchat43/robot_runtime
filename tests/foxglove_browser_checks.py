"""Inspect real Foxglove startup settings through the existing browser panel."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import base64

from integration_browser import wait_for


async def exercise(cdp, local, area, check):
    group = 'document.querySelector(\'[data-parameter-group="foxglove-settings"]\')'
    await wait_for(lambda: cdp.evaluate(f'!!{group} && !{group}.querySelector("button.secondary").disabled'))
    await cdp.evaluate(f'{group}.querySelector("button.secondary").click(); true')
    await wait_for(lambda: cdp.evaluate(f'{group}.querySelector(".ros-result").textContent.includes("Values read.")'))
    values = await local("ros_parameters", capability_id="foxglove-settings", refresh=False)
    assert values["fresh"] and len(values["parameters"]) == 15
    assert values["parameters"]["port"]["value"] == (area / "bridge-port.txt").read_text()
    assert await cdp.evaluate(f'[...{group}.querySelectorAll(".parameter-name input")].every(input=>input.disabled)')
    assert await cdp.evaluate('document.querySelector(\'[data-parameter="capabilities"] .parameter-observed\').textContent.includes("clientPublish")')
    await cdp.call("Emulation.setDeviceMetricsOverride", width=900, height=1000, deviceScaleFactor=1, mobile=False)
    await cdp.evaluate(f'{group}.scrollIntoView(); true')
    assert not await cdp.evaluate('document.documentElement.scrollWidth>innerWidth')
    shot = await cdp.call("Page.captureScreenshot", format="png")
    (area / "foxglove-900.png").write_bytes(base64.b64decode(shot["data"]))
    check("foxglove_fifteen_readonly_settings_and_900px_layout", settings=values)
