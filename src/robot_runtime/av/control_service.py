# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
"""Independent, camera-free WebRTC diagnostic input sessions.

The only output is GamepadReceipt. There is deliberately no actuator adapter.
The HTTP worker negotiates; GStreamer callbacks never tear down a pipeline.
"""
import json
import secrets
import threading
import time

from .gamepad import CHANNEL, GamepadReceipt, compact_ack
from .gst_runtime import Gst, GstSdp, GstWebRTC, restrict_ice
from .stream_settings import SettingsConflict


class ControlService:
    def __init__(self, args, output, authorized):
        self.args = args
        self.lock = threading.RLock()
        self.receiver = GamepadReceipt(output, policy=vars(args))
        self.peers = {}
        self.closed = False
        self.ack_skipped = 0
        self.allow_enable = lambda: True
        self.authorized = authorized

    def paused(self):
        return self.receiver.paused()

    def _promise(self, element, signal, *values):
        ready = threading.Event()
        promise = Gst.Promise.new_with_change_func(lambda *_: ready.set(), None, None)
        element.emit(signal, *values, promise)
        if not ready.wait(self.args.control_negotiate_timeout_s):
            promise.interrupt()
            raise TimeoutError('Control negotiation timed out')
        reply = promise.get_reply()
        if reply and reply.has_field('error'):
            raise ValueError(str(reply.get_value('error')))
        return reply

    def offer(self, value, identity):
        mode, sdp = value.get('mode'), value.get('sdp')
        if mode not in ('input', 'fpv'):
            raise ValueError('Control mode must be input or fpv')
        if not isinstance(sdp, str) or not 10 <= len(sdp) <= 64000:
            raise ValueError('Invalid control offer')
        if 'm=application ' not in sdp or 'm=video ' in sdp or 'm=audio ' in sdp:
            raise ValueError('A control connection must contain only a data channel')
        with self.lock:
            if self.closed:
                raise SettingsConflict('Control service is stopping')
            used = {peer['port'] for peer in self.peers.values()}
            ports = range(self.args.control_port, self.args.control_port + self.args.max_control_sessions)
            port = next((item for item in ports if item not in used), None)
            if port is None:
                raise SettingsConflict('The configured control session limit has been reached')
            if identity in self.peers or not self.authorized(identity):
                raise SettingsConflict('Input session expired or already exists')
            peer = {'mode': mode, 'port': port, 'created': time.monotonic(), 'last': None,
                    'finished': False, 'pipeline': None, 'rtc': None, 'channel': None,
                    'state': 'negotiating', 'handlers': []}
            self.peers[identity] = peer
        try:
            pipeline = Gst.Pipeline.new('diagnostic-input-' + str(port))
            rtc = Gst.ElementFactory.make('webrtcbin', None)
            if rtc is None:
                raise RuntimeError('Missing webrtcbin')
            peer.update(pipeline=pipeline, rtc=rtc)
            rtc.set_property('bundle-policy', GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
            rtc.set_property('latency', 0)
            peer['ice'] = restrict_ice(rtc, self.args.host, port)
            pipeline.add(rtc)
            self._connect(peer, rtc, 'on-data-channel', lambda _, channel: self._channel(identity, channel))
            self._connect(peer, rtc, 'notify::connection-state', lambda *_: self._connection(identity))
            gathered = threading.Event()
            self._connect(peer, rtc, 'notify::ice-gathering-state', lambda *_: gathered.set()
                if rtc.get_property('ice-gathering-state') == GstWebRTC.WebRTCICEGatheringState.COMPLETE else None)
            if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError('Could not start control connection')
            result, message = GstSdp.SDPMessage.new_from_text(sdp)
            if result != GstSdp.SDPResult.OK:
                raise ValueError('Invalid SDP')
            remote = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.OFFER, message)
            self._promise(rtc, 'set-remote-description', remote)
            reply = self._promise(rtc, 'create-answer', None)
            answer = reply.get_value('answer') if reply else None
            if answer is None:
                raise ValueError('Could not create control answer')
            self._promise(rtc, 'set-local-description', answer)
            if rtc.get_property('ice-gathering-state') != GstWebRTC.WebRTCICEGatheringState.COMPLETE:
                if not gathered.wait(self.args.control_negotiate_timeout_s):
                    raise TimeoutError('Control ICE gathering timed out')
            local = rtc.get_property('local-description')
            return {'session_id': identity, 'sdp': local.sdp.as_text(), 'mode': mode}
        except Exception:
            self.remove(identity, 'negotiation failed')
            raise

    @staticmethod
    def _connect(peer, element, signal, handler):
        peer['handlers'].append((element, element.connect(signal, handler)))

    def _connection(self, identity):
        with self.lock:
            peer = self.peers.get(identity)
        if peer:
            state = peer['rtc'].get_property('connection-state').value_nick
            with self.lock:
                peer['state'] = state
            print(json.dumps({'event': 'control_state',
                'mono_ms': time.monotonic() * 1000, 'state': state,
                'ice_state': peer['rtc'].get_property('ice-connection-state').value_nick}), flush=True)
            if state in ('failed', 'closed', 'disconnected'):
                self._finish(identity, 'connection ' + state)

    def _channel(self, identity, channel):
        # Accept exactly the agreed unordered, unretransmitted snapshot channel.
        if (channel.get_property('label') != CHANNEL or channel.get_property('ordered') or
                channel.get_property('max-retransmits') != 0):
            channel.emit('close')
            return
        with self.lock:
            peer = self.peers.get(identity)
            if peer is None or peer['channel'] is not None or peer['finished']:
                reject = True
            else:
                reject = False
                peer['channel'] = channel
                self._connect(peer, channel, 'on-open', lambda *_: self._open(identity, channel))
                self._connect(peer, channel, 'on-message-string', lambda _, text: self._message(identity, channel, text))
                self._connect(peer, channel, 'on-close', lambda *_: self._finish(identity, 'data channel closed'))
        if reject:
            channel.emit('close')

    def _open(self, identity, channel):
        with self.lock:
            peer = self.peers.get(identity)
            if not peer or peer['finished'] or not self.authorized(identity):
                return
            peer['last'] = time.monotonic()
            hello = self.receiver.open(identity, require_video=peer['mode'] == 'fpv')
        channel.emit('send-string', json.dumps(hello, separators=(',', ':')))

    def _message(self, identity, channel, text):
        try:
            with self.lock:
                peer = self.peers.get(identity)
                if not peer or peer['finished'] or not self.authorized(identity):
                    return
                ack = self.receiver.receive(identity, text, allow_enable=self.allow_enable())
                peer['last'] = time.monotonic()
            if ack and channel.get_property('ready-state') == GstWebRTC.WebRTCDataChannelState.OPEN:
                if channel.get_property('buffered-amount') > 0:
                    self.ack_skipped += 1
                    return
                payload = compact_ack(ack) if ack['type'] == 'gamepad-ack' else ack
                channel.emit('send-string', json.dumps(payload, separators=(',', ':')))
        except (ValueError, TypeError):
            pass

    def _finish(self, identity, reason):
        with self.lock:
            peer = self.peers.get(identity)
            if peer and not peer['finished']:
                peer['finished'] = True
                peer['finish_reason'] = reason
                self.receiver.remove(identity)

    def remove(self, identity, reason='client closed'):
        with self.lock:
            peer = self.peers.pop(identity, None)
            self.receiver.remove(identity)
        if peer:
            now = time.monotonic()
            print(json.dumps({'event': 'control_closed', 'mono_ms': now * 1000,
                'reason': peer.get('finish_reason', reason), 'state': peer['state'],
                'lifetime_ms': (now - peer['created']) * 1000,
                'last_received_age_ms': (now - peer['last']) * 1000 if peer['last'] is not None else None}), flush=True)
            for element, handler in peer['handlers']:
                element.disconnect(handler)
            if peer['pipeline']:
                peer['pipeline'].set_state(Gst.State.NULL)

    def check(self):
        now = time.monotonic()
        with self.lock:
            stale = []
            for identity, peer in self.peers.items():
                if not self.authorized(identity):
                    stale.append((identity, 'runtime authorization expired'))
                elif peer['finished']:
                    stale.append((identity, peer['finish_reason']))
                elif peer['last'] is not None and now - peer['last'] > self.args.control_idle_timeout_s:
                    stale.append((identity, 'input inactivity timeout'))
                elif peer['last'] is None and now - peer['created'] > self.args.control_negotiate_timeout_s * 3:
                    stale.append((identity, 'negotiation timeout'))
        for identity, reason in stale:
            self.remove(identity, reason)

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            return {identity: {'mode': peer['mode'], 'state': peer['state'], 'port': peer['port'],
                    'lifetime_ms': (now - peer['created']) * 1000,
                    'last_received_age_ms': (now - peer['last']) * 1000 if peer['last'] is not None else None}
                    for identity, peer in self.peers.items()}

    def close(self):
        with self.lock:
            self.closed = True
            identities = list(self.peers)
        for identity in identities:
            self.remove(identity, 'server stopping')
        self.receiver.close()
