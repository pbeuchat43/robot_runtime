"""Native video/input independently and together in the packaged HTTPS browser."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import asyncio
import base64
import contextlib
import importlib.metadata
import json
import os
import time
import uuid

from robot_runtime.auth import write_credential
from integration_browser import CDP, wait_for


async def exercise(reference, devtools, debug_port, origin, local, area, check,
                   api, command_headers, passphrase, restart_manager, restart_web):
    target = (await reference.call('Target.createTarget', url='about:blank'))['targetId']
    async with devtools.get(f'http://127.0.0.1:{debug_port}/json/list') as response:
        websocket = next(item['webSocketDebuggerUrl'] for item in await response.json() if item['id'] == target)
    connection = await devtools.ws_connect(websocket)
    page = CDP(connection)
    await page.call('Runtime.enable')
    await page.call('Emulation.setDeviceMetricsOverride', width=1200, height=1100, deviceScaleFactor=1, mobile=False)
    await page.call('Page.bringToFront')
    # Headless focus is explicit fixture evidence; physical input is tested separately.
    await page.call('Emulation.setFocusEmulationEnabled', enabled=True)
    details = dict(package_version=importlib.metadata.version('robot-runtime'),
                   evidence='Native WebRTC and hardware encoder; synthetic source and synthetic browser input', phases=[])

    async def status(identity):
        return next(u for u in (await local('status', fresh=True))['units'] if u['unit_id'] == identity)

    async def stats(identity):
        code, result, _ = await api('/api/av/' + identity + '/stats')
        return result if code == 200 else None

    async def empty(identity):
        value = await stats(identity)
        return value is not None and value['admission']['subscriptions'] == 0 and not value.get('control_sessions') and not value.get('consumers')

    async def command(identity, action):
        current = await status(identity)
        value = await local(action, unit_id=identity, request_id=str(uuid.uuid4()),
            expires_at_ms=int(time.time() * 1000) + 10000,
            config_revision=(await local('status'))['config_revision'],
            **({'expected_run': current['run_id']} if action == 'stop' else {}))
        return value

    async def post(identity, route, arguments, headers=command_headers):
        code, config, _ = await api('/api/av/' + identity + '/config')
        assert code == 200, config
        current = config['runtime']
        return await api('/api/av/' + identity + '/' + route, 'POST', dict(
            request_id=str(uuid.uuid4()), expires_at_ms=int(time.time() * 1000) + 10000,
            expected_run=current['run_id'], config_revision=current['config_revision'], generation=current['generation'],
            source_run=current['source_run'], arguments=arguments), headers)

    async def navigate(path):
        await page.call('Page.navigate', url=origin + path)
        await wait_for(lambda: page.evaluate("!!document.querySelector('#input-status') && document.querySelector('#auth-status').textContent==='Commands unlocked'"))

    async def click(selector):
        await page.evaluate(f'document.querySelector({json.dumps(selector)}).click(); true')

    async def connect_input():
        await click('#receipt [data-ref=connect]')
        await wait_for(lambda: page.evaluate("document.querySelector('#receipt [data-ref=status]').textContent==='connected'"), seconds=25)
        await page.evaluate("document.querySelector('#gamepad [data-ref=simulation]').checked=true; document.querySelector('#gamepad [data-ref=simulation]').dispatchEvent(new Event('change')); true")
        await wait_for(lambda: page.evaluate("!document.querySelector('#receipt [data-ref=enabled]').disabled"))
        await click('#receipt [data-ref=enabled]')
        await wait_for(lambda: page.evaluate("document.querySelector('#receipt [data-ref=receipt]').textContent.includes('Jetson confirmed')"))

    async def connect_video():
        await click('#video [data-ref=connect]')
        await wait_for(lambda: page.evaluate("document.querySelector('video').videoWidth>0 && document.querySelector('#video [data-ref=status]').textContent==='connected'"), seconds=35)

    async def shot(name):
        value = await page.call('Page.captureScreenshot', format='png')
        (area / name).write_bytes(base64.b64decode(value['data']))

    async def browser_login():
        await page.evaluate("fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({passphrase:" +
                            json.dumps(passphrase) + "})}).then(r=>r.json()).then(s=>s.authenticated)")
        await navigate('/camera-gamepad')

    async def both():
        await connect_input(); await connect_video()
        await wait_for(lambda: page.evaluate("document.querySelector('#receipt [data-ref=receipt]').textContent.includes('Jetson confirmed')"))

    try:
        await navigate('/input')
        assert (await status('input'))['lifecycle'] == (await status('video'))['lifecycle'] == 'stopped'
        await click('#input-start')
        await wait_for(lambda: stats('input'))
        await connect_input()
        value = await stats('input')
        assert value['gamepad'][0]['source'] == 'simulation' and value['gamepad'][0]['fresh'] and not value['actuators_enabled']
        assert 'camera' not in value and (await status('video'))['run_id'] is None
        await page.evaluate("window.dispatchEvent(new Event('blur')); true")
        await wait_for(lambda: page.evaluate("!document.querySelector('#receipt [data-ref=enabled]').checked"))
        await shot('av-input.png')
        check('input_alone_real_datachannel_receipt_pause_and_no_media_workload', source='simulation')
        await click('#receipt [data-ref=disconnect]')
        await wait_for(lambda: empty('input'))

        await navigate('/video')
        await click('#media-start')
        await wait_for(lambda: stats('video'), seconds=25)
        await connect_video()
        value = await stats('video')
        assert value['consumers'] and not (await stats('input'))['control_sessions']
        details['phases'].append(dict(name='video-only', stats=value))
        await shot('av-video.png')
        check('video_alone_real_h264_webrtc_frames_and_no_input_peer')

        # Admission is enforced before native creation, and tokens retain owners.
        grants = []
        try:
            for _ in range(4):
                code, grant, _ = await post('video', 'media/sessions', {'profile': 'current'})
                assert code == 200, grant
                grants.append(grant['token'])
            code, denied, _ = await post('video', 'media/sessions', {'profile': 'current'})
            assert code == 409 and denied['error']['code'] == 'busy', denied
            foreign = await page.evaluate("""(async()=>{
              const s=await fetch('/api/session').then(r=>r.json()), c=await fetch('/api/av/video/config').then(r=>r.json());
              const r=await fetch('/api/av/video/media/close',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':s.csrf},
                body:JSON.stringify({request_id:crypto.randomUUID(),expires_at_ms:Date.now()+10000,
                  expected_run:c.runtime.run_id,config_revision:c.runtime.config_revision,generation:c.runtime.generation,
                  source_run:c.runtime.source_run,arguments:{token:TOKEN}})});
              return {status:r.status,body:await r.json()};
            })()""".replace('TOKEN', json.dumps(grants[0])))
            assert foreign['status'] == 403, foreign
            check('bounded_admission_before_native_video_and_foreign_owner_rejected')
        finally:
            for token in grants:
                await post('video', 'media/close', {'token': token})
        await click('#video [data-ref=disconnect]')
        await wait_for(lambda: empty('video'))
        async def camera_idle():
            value = await stats('video')
            return value if value and value['camera']['state'] == 'idle' else None
        idle = await wait_for(camera_idle, seconds=10)
        details['phases'].append(dict(name='last-video-released', stats=idle))
        check('last_video_subscriber_releases_capture_and_encoders')

        await navigate('/camera-gamepad')
        await both()
        input_before = await stats('input')
        code, busy, _ = await post('video', 'settings', {})
        assert code == 409 and busy['error']['code'] == 'busy'
        await shot('av-combined.png')
        details['phases'].append(dict(name='combined', video=await stats('video'), input=input_before))
        await click('#video [data-ref=disconnect]')
        await wait_for(lambda: empty('video'))
        _, config, _ = await api('/api/av/video/config')
        settings = dict(config['settings']['applied'], fps=10)
        code, result, _ = await post('video', 'settings', dict(revision=config['settings']['revision'], settings=settings))
        assert code == 200, result
        async def applied():
            value = await stats('video')
            return value and not value['settings']['applying'] and value['settings']['applied']['fps'] == 10
        await wait_for(applied)
        await connect_video()
        input_after = await stats('input')
        assert input_after['gamepad'][0]['last_seq'] > input_before['gamepad'][0]['last_seq'] and input_after['gamepad'][0]['fresh']
        assert input_after['control_sessions'][0]['lifetime_ms'] > input_before['control_sessions'][0]['lifetime_ms']
        check('combined_media_reconfiguration_preserves_independent_input_session')

        # Revoke on the server without asking the clients to disconnect first.
        await page.evaluate("fetch('/api/session').then(r=>r.json()).then(s=>fetch('/api/logout',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':s.csrf},body:'{}'})).then(r=>r.status)")
        await wait_for(lambda: empty('input'), seconds=7)
        await wait_for(lambda: empty('video'), seconds=7)
        await asyncio.sleep(1)
        assert await empty('input') and await empty('video')
        check('server_logout_revokes_native_sessions_without_client_cooperation')

        await browser_login(); await both()
        runs = {identity: (await status(identity))['run_id'] for identity in ('input', 'video')}
        await restart_manager()
        await wait_for(lambda: empty('input'), seconds=7)
        await wait_for(lambda: empty('video'), seconds=7)
        assert runs == {identity: (await status(identity))['run_id'] for identity in runs}
        check('manager_generation_change_revokes_sessions_preserves_owned_services')

        await navigate('/camera-gamepad'); await both()
        await restart_web()
        await wait_for(lambda: empty('input'), seconds=7)
        await wait_for(lambda: empty('video'), seconds=7)
        assert runs == {identity: (await status(identity))['run_id'] for identity in runs}
        check('web_restart_revokes_native_sessions_preserves_owned_services')

        await browser_login(); await both()
        write_credential(area / 'credential-replacement.json', passphrase)
        os.replace(area / 'credential-replacement.json', area / 'credential.json')
        await wait_for(lambda: empty('input'), seconds=7)
        await wait_for(lambda: empty('video'), seconds=7)
        check('credential_rotation_revokes_native_sessions')

        for identity in ('input', 'video'):
            await command(identity, 'stop')
        async def stopped():
            return all(not u['blocked'] and not u['members'] and not u['main_pid'] for u in
                       (await local('status', fresh=True))['units'] if u['unit_id'] in ('input', 'video'))
        await wait_for(stopped, seconds=15)
        check('native_services_stop_gracefully_with_empty_owned_cgroups')
        assert not page.exceptions, page.exceptions
        details['passed'] = True
    except BaseException:
        with contextlib.suppress(Exception):
            details['failure'] = dict(page=await page.evaluate('document.body.innerText'), video=await stats('video'),
                                      input=await stats('input'), exceptions=page.exceptions)
            await shot('av-failure.png')
        raise
    finally:
        (area / 'av-details.json').write_text(json.dumps(details, indent=2))
        await reference.call('Target.closeTarget', targetId=target)
        await connection.close()
    # Re-establish the harness's two independent sessions after revocation checks.
    code, session, _ = await api('/api/login', 'POST', dict(passphrase=passphrase))
    assert code == 200
    command_headers['X-CSRF-Token'] = session['csrf']
    await reference.call('Page.bringToFront')
    await reference.call('Page.reload')
    await wait_for(lambda: reference.evaluate('!!document.querySelector("tr[data-unit=idle]")'))
    await reference.evaluate('document.getElementById("passphrase").value=' + json.dumps(passphrase) + '; document.querySelector("#unlock button").click(); true')
    await wait_for(lambda: reference.evaluate('document.getElementById("unlock").hidden'))
