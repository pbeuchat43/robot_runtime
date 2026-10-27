"""Camera/input authority, profile and signalling boundaries; no device access."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import asyncio
import copy
import json
import signal
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import ClientSession, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from robot_runtime.auth import COOKIE, write_credential
from robot_runtime.av.sessions import Sessions
from robot_runtime.av.worker import Worker, declared_camera
from robot_runtime.av.cameras import oak_worker
from robot_runtime.av.signalling import Signalling, peer_payload
from robot_runtime.av_profile import current_target
from robot_runtime.av_web import AVGateway
from robot_runtime.common import Fault
from robot_runtime.profile import validate
from robot_runtime.web import application
from test_core import profile


def fixture():
    value = profile()
    value['units'].append(dict(value['units'][0], id='input'))
    value['av'] = {'services': [dict(id='video', label='Video', kind='media', unit_id='worker',
        host='127.0.0.1', udp_port=41000, source={'kind': 'test', 'label': 'Software pattern'}),
        dict(id='input', label='Input', kind='input', unit_id='input', host='127.0.0.1', udp_port=41010)]}
    return value


def choices_fixture():
    value = fixture()
    value['units'][1]['launch_modes'] = [dict(id=m, label=m, argv=['/bin/true']) for m in ('cones', 'coco', 'both')]
    value['av']['services'][0]['source'] = dict(kind='ros', label='Overlays', owner_unit='input', domain_id=230,
        default_choice='cones', choices=[dict(id=m, label=m, topic='/detector_' + m + '/overlay/image', active_modes=[m, 'both'])
                                        for m in ('cones', 'coco')])
    return value


class CaptureSignalTests(unittest.TestCase):
    def test_signal_during_shared_event_wait_does_not_reenter_its_lock(self):
        handlers = {}
        stopped, errors, capture = Mock(), Mock(), Mock()
        def interrupted_wait(_timeout):
            # Models a signal delivered while multiprocessing.Event owns its
            # non-reentrant condition lock. Re-entry would deadlock in production.
            stopped.set.side_effect = AssertionError('Shared lock re-entered')
            handlers[signal.SIGINT](signal.SIGINT, None)
            return False
        stopped.wait.side_effect = interrupted_wait
        with patch('robot_runtime.av.cameras.signal.signal', side_effect=lambda sig, fn: handlers.update({sig: fn})), \
             patch('robot_runtime.av.cameras.NativeOakCapture', return_value=capture):
            oak_worker({}, 2, 2, 5, bytearray(6), Mock(), Mock(), Mock(), stopped, errors, Mock())
        stopped.set.assert_not_called()
        capture.read.assert_not_called()
        capture.close.assert_called_once()
        errors.put_nowait.assert_not_called()


class ProfileTests(unittest.TestCase):
    def test_declared_ros_choices_are_bounded_and_apply_to_owner_launches(self):
        config = validate(choices_fixture(), check_paths=False)[0]
        cameras, _ = declared_camera(config['av']['services'][0]['source'])
        self.assertEqual([c['id'] for c in cameras], ['cones', 'coco'])
        self.assertEqual(cameras[1]['topic'], '/detector_coco/overlay/image')
        for edit in (lambda s: s.update(choices=[]), lambda s: s.update(default_choice='missing'),
                     lambda s: s.update(topic='/also'), lambda s: s['choices'].append(s['choices'][0]),
                     lambda s: s['choices'][0].update(topic='relative'),
                     lambda s: s['choices'][0].update(active_modes=['missing']),
                     lambda s: s['choices'][0].update(active_modes=['both']),
                     lambda s: s['choices'][0].update(active_modes=['cones', 'cones']),
                     lambda s: s['choices'][0].update(active_modes='both')):
            value = choices_fixture(); edit(value['av']['services'][0]['source'])
            with self.assertRaises(Fault): validate(value, check_paths=False)

    def test_optional_bounded_services_and_exact_physical_ownership(self):
        self.assertNotIn('av', validate(profile(), check_paths=False)[0])
        config = validate(fixture(), check_paths=False)[0]
        self.assertEqual(config['av']['services'][0]['max_sessions'], 5)
        for edit in (lambda s: s.update(max_sessions=6), lambda s: s.update(source=[]),
                     lambda s: s.update(host='0.0.0.0'), lambda s: s.update(source={'kind': 'v4l2', 'label': 'X', 'path': '/dev/video0'}),
                     lambda s: s.update(profiles={'observer': {'source': 'auto'}})):
            value = fixture(); edit(value['av']['services'][0])
            with self.assertRaises(Fault): validate(value, check_paths=False)
        value = fixture()
        value['av']['services'][1]['udp_port'] = 41002
        with self.assertRaises(Fault): validate(value, check_paths=False)
        value = fixture()
        first = value['av']['services'][0]
        first['source'] = dict(kind='oak', label='OAK', serial='ABCDE')
        value['av']['services'][1] = dict(first, id='second', unit_id='input', udp_port=41010)
        with self.assertRaises(Fault): validate(value, check_paths=False)
        value['units'][0]['conflicts'], value['units'][1]['conflicts'] = ['input'], ['worker']
        validate(value, check_paths=False)

    def test_target_requires_fresh_owned_run_and_current_ros_source_mode(self):
        config = validate(fixture(), check_paths=False)[0]
        unit = dict(unit_id='worker', lifecycle='running', ownership='owned', run_id='a' * 32)
        source = dict(unit_id='input', lifecycle='running', ownership='owned', run_id='b' * 32,
                      launch_mode={'id': 'rgb'})
        status = dict(fresh=True, units=[unit, source])
        core = SimpleNamespace(profile=config, revision='rev', generation='gen', status=lambda: status,
                               store=SimpleNamespace(directory=Path('/tmp/state')))
        target = current_target(core, 'video')
        self.assertTrue(target['socket'].endswith('av-video-aaaaaaaaaaaa.sock'))
        for key, bad in [('lifecycle', 'starting'), ('ownership', 'conflict'), ('stop_request_id', 'pending')]:
            old = unit.get(key); unit[key] = bad
            with self.assertRaises(Fault): current_target(core, 'video')
            unit[key] = old
        config['av']['services'][0]['source'] = dict(kind='ros', owner_unit='input', required_mode='rgb')
        self.assertEqual(current_target(core, 'video')['source_run'], 'b' * 32)
        source['launch_mode'] = None
        with self.assertRaises(Fault): current_target(core, 'video')
        status['fresh'] = False
        with self.assertRaises(Fault): current_target(core, 'input')


class SourceChoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config, revision = validate(choices_fixture(), check_paths=False)
        self.worker = Worker(config, revision, config['av']['services'][0], 'a' * 32, Path(self.temp.name))
        cameras = SimpleNamespace(selected='cones')
        def select(identity):
            self.assertFalse(self.worker.sessions.entries, 'All subscriptions revoked before source selection')
            cameras.selected = identity
        self.worker.stream = SimpleNamespace(cameras=cameras, select_camera=Mock(side_effect=select),
            settings=SimpleNamespace(snapshot=lambda: dict(applying=False)), args=SimpleNamespace(profile='observer'), consumers={})
        self.context = dict(gateway='web', owner='alice', source_run='b' * 32, source_mode='both')
        await self.worker.bind_source(self.context)

    async def test_switch_revokes_all_viewers_and_rejects_stale_or_unknown_choices(self):
        worker = self.worker
        first, _, _ = worker.sessions.acquire('media', self.context, 'first', {})
        second, _, _ = worker.sessions.acquire('media', dict(self.context, owner='bob'), 'second', {})
        revision = worker.source_revision
        for body in (dict(id='coco', source_revision=revision-1), dict(id='missing', source_revision=revision)):
            with self.assertRaises(Fault): await worker.operation('camera', body, self.context, 'switch')
            self.assertEqual(set(worker.sessions.entries), {first, second})
            self.assertEqual(worker.stream.cameras.selected, 'cones')
        await worker.operation('camera', dict(id='coco', source_revision=revision), self.context, 'switch')
        self.assertFalse(worker.sessions.entries)
        self.assertEqual(worker.stream.cameras.selected, 'coco')
        self.assertEqual(worker.source_revision, revision+1)
        self.assertEqual(worker.source_binding, ('b' * 32, 'both'))
        with self.assertRaises(Fault):
            await worker.operation('media/sessions', dict(profile='current', source_revision=revision), self.context, 'late')
        self.assertFalse(worker.sessions.entries)

    async def test_single_model_selects_available_overlay_and_rejects_inactive_choice(self):
        context = dict(self.context, source_run='c' * 32, source_mode='coco')
        await self.worker.bind_source(context)
        self.assertEqual(self.worker.stream.cameras.selected, 'coco')
        self.assertEqual([c['id'] for c in self.worker.source_choices() if c['available']], ['coco'])
        with self.assertRaises(Fault):
            await self.worker.operation('camera', dict(id='cones', source_revision=self.worker.source_revision), context, 'off')
        revision = self.worker.source_revision
        await self.worker.bind_source(context)
        self.assertEqual(self.worker.source_revision, revision)


class SessionTests(unittest.TestCase):
    def test_dedup_owner_bounds_expiry_and_no_resurrection(self):
        now = [10.]
        sessions = Sessions(1, clock=lambda: now[0])
        context = dict(gateway='web', owner='alice', source_run=None)
        token, entry, created = sessions.acquire('media', context, 'one', {'profile': 'observer'})
        self.assertTrue(created)
        self.assertEqual(sessions.acquire('media', context, 'one', {'profile': 'observer'})[0], token)
        for foreign in (dict(context, owner='bob'), dict(context, gateway='other'), dict(context, source_run='new')):
            with self.assertRaises(Fault): sessions.require(token, foreign)
        with self.assertRaises(Fault): sessions.acquire('media', context, 'two', {})
        with self.assertRaises(Fault): sessions.acquire('media', context, 'one', {'profile': 'teleop'})
        now[0] += 3.01
        sessions.heartbeat('web', {'alice'}, None)
        self.assertFalse(sessions.allowed(token))
        with self.assertRaises(Fault): sessions.renew(token, context)
        with self.assertRaises(Fault): sessions.acquire('media', context, 'new', {})
        sessions.remove(token)
        second, _, _ = sessions.acquire('input', context, 'new', {})
        sessions.heartbeat('web', set(), None)
        self.assertFalse(sessions.allowed(second))

    def test_media_lease_expires_even_if_browser_authentication_is_live(self):
        now = [0.]; sessions = Sessions(2, clock=lambda: now[0])
        context = dict(gateway='web', owner='alice', source_run=None)
        video, _, _ = sessions.acquire('media', context, 'video', {})
        gamepad, _, _ = sessions.acquire('input', context, 'input', {})
        for step in range(1, 22):
            now[0] = step
            sessions.heartbeat('web', {'alice'}, None)
        self.assertFalse(sessions.allowed(video))
        self.assertTrue(sessions.allowed(gamepad))
        sessions.invalidate('web')
        self.assertFalse(sessions.allowed(gamepad))

    def test_consumer_cannot_send_audio_data_or_publishing_sdp(self):
        for sdp in ('m=video 9 UDP/TLS/RTP/SAVPF 96\r\na=sendrecv\r\n',
                    'm=video 9 UDP/TLS/RTP/SAVPF 96\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n'):
            with self.assertRaises(Fault):
                peer_payload(dict(type='peer', sessionId='x', sdp=dict(type='answer', sdp=sdp)), producer=False)
        peer_payload(dict(type='peer', sessionId='x', sdp=dict(type='answer',
                     sdp='m=video 9 UDP/TLS/RTP/SAVPF 96\r\na=recvonly\r\n')), producer=False)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        write_credential(root / 'credential.json', 'isolated AV test passphrase')
        self.app = application(root, 'https://robot.test:8443', root / 'credential.json')
        self.adapter = self.app['adapter']
        self.target = dict(id='video', kind='media', unit_id='worker', run_id='a' * 32,
                           config_revision='rev', generation='gen', source_run=None, socket='/tmp/missing.sock')
        self.adapter.rpc = AsyncMock(return_value=self.target)
        self.adapter.av.call = AsyncMock(return_value={'token': 'grant'})
        self.client = TestClient(TestServer(self.app)); await self.client.start_server()
        self.headers = {'Host': 'robot.test:8443', 'Origin': 'https://robot.test:8443'}
        token, session = self.adapter.auth.issue()
        self.auth = dict(self.headers, Cookie=f'{COOKIE}={token}', **{'X-CSRF-Token': session['csrf']})
        self.owner = session['id']
        self.body = dict(request_id=str(uuid.uuid4()), expires_at_ms=int(time.time() * 1000) + 10000,
                         expected_run='a' * 32, config_revision='rev', generation='gen', source_run=None,
                         arguments={'profile': 'observer'})

    async def asyncTearDown(self):
        await self.client.close(); self.temp.cleanup()

    async def test_authenticated_run_bound_commands_and_private_routes(self):
        path = '/api/av/video/media/sessions'
        for headers, expected in [(self.headers, 401), (dict(self.auth, Origin='https://foreign.test'), 403),
                                  (dict(self.auth, **{'X-CSRF-Token': 'wrong'}), 403)]:
            result = await self.client.post(path, json=self.body, headers=headers)
            self.assertEqual(result.status, expected)
        self.adapter.av.call.assert_not_called()
        result = await self.client.post(path, json=dict(self.body, expected_run='b' * 32), headers=self.auth)
        self.assertEqual(result.status, 409)
        result = await self.client.post(path, json=self.body, headers=self.auth)
        self.assertEqual(result.status, 200)
        self.assertEqual(self.adapter.av.call.call_args.args[3], self.owner)
        result = await self.client.post('/api/av/video/heartbeat', json=self.body, headers=self.auth)
        self.assertEqual(result.status, 404)
        result = await self.client.get('/api/av/video/signal?token=test', headers=self.headers)
        self.assertEqual(result.status, 401)

    async def test_assets_are_packaged_and_no_path_traversal(self):
        for path in ('/video', '/input', '/camera-gamepad', '/assets/av/page.js',
                     '/assets/av/runtime-api.js', '/assets/av/vendor/gstwebrtc-api-3.0.0.esm.js'):
            response = await self.client.get(path, headers=self.headers)
            self.assertEqual(response.status, 200, path)
        response = await self.client.get('/assets/av/not-a-file.js', headers=self.headers)
        self.assertEqual(response.status, 404)


class SignallingTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_producer_and_admitted_consumers_cannot_escalate_roles(self):
        sessions = Sessions(2)
        context = dict(gateway='web', owner='alice', source_run=None)
        token, _, _ = sessions.acquire('media', context, 'one', {})
        signalling = Signalling(sessions)
        uri = await signalling.start()
        async def consumer(request):
            return await signalling.consumer_connection(request, token, context)
        app = web.Application(); app.router.add_get('/signal', consumer)
        client = TestClient(TestServer(app)); await client.start_server()
        try:
            async with ClientSession() as native:
                async with native.get(uri.replace('ws:', 'http:')) as response:
                    self.assertEqual(response.status, 403)
                async with native.ws_connect(uri, headers={'Authorization': 'Bearer ' + signalling.secret}) as producer:
                    await producer.receive_json()
                    await producer.send_json(dict(type='setPeerStatus', roles=['producer']))
                    await producer.receive_json()
                    async with client.ws_connect('/signal') as viewer:
                        await viewer.receive_json()
                        await viewer.send_json(dict(type='setPeerStatus', roles=['producer']))
                        self.assertEqual((await viewer.receive(timeout=2)).type, WSMsgType.CLOSE)
                    self.assertFalse(signalling.media)
                    async with client.ws_connect('/signal') as viewer:
                        await viewer.receive_json()
                        await viewer.send_json(dict(type='startSession', peerId=signalling.producer_id))
                        started = await viewer.receive_json()
                        self.assertEqual(started['type'], 'sessionStarted')
                        self.assertEqual((await producer.receive_json())['type'], 'startSession')
                        await viewer.send_json(dict(type='endSession', sessionId='foreign'))
                        self.assertEqual((await viewer.receive(timeout=2)).type, WSMsgType.CLOSE)
        finally:
            await client.close(); await signalling.close()


class NativeReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_observation_error_field_is_not_an_http_error_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            value, status = {'error': None}, 200
            async def reply(_request):
                return web.json_response(value, status=status)
            app = web.Application(); app.router.add_post('/call', reply)
            runner = web.AppRunner(app); await runner.setup()
            endpoint = str(Path(directory) / 'native.sock')
            await web.UnixSite(runner, endpoint).start()
            gateway = AVGateway(SimpleNamespace(generation='web'))
            target = dict(socket=endpoint, run_id='a' * 32, config_revision='rev', source_run=None)
            try:
                self.assertEqual(await gateway.call(target, 'stats'), {'error': None})
                value = {'error': 'Camera disconnected', 'actuators_enabled': False}
                self.assertEqual(await gateway.call(target, 'stats'), value)
                value, status = {'error': {'code': 'stale_run', 'message': 'Changed'}}, 400
                with self.assertRaises(Fault) as caught:
                    await gateway.call(target, 'stats')
                self.assertEqual(caught.exception.code, 'stale_run')
            finally:
                await runner.cleanup()
