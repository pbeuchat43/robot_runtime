# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
"""Bounded diagnostic gamepad receipt. Deliberately has no actuator/ROS interface."""
import json
import math
import secrets
import threading
import time
from .diagnostics import JsonLog
from .settings_config import load_profile

CHANNEL = 'robot-gamepad-v2'


def compact_ack(ack):
    """Full input stays in the host log; only receipt/preview goes on the wire."""
    return {key: (round(ack[key], 3) if key == 'sent_ms' else ack[key])
            for key in ('type', 'seq', 'sent_ms', 'active', 'enabled', 'accepted', 'reason',
                        'source', 'preview', 'actuators_enabled', 'epoch', 'lease', 'input_age_upper_bound_ms')}


def validate_packet(text):
    if not isinstance(text, str) or len(text) > 4096:
        raise ValueError('Gamepad packet exceeds 4096 characters')
    packet = json.loads(text)
    if not isinstance(packet, dict) or packet.get('type') != 'gamepad' or type(packet.get('v')) is not int or packet['v'] != 2:
        raise ValueError('Unsupported gamepad packet')
    seq = packet.get('seq')
    if type(seq) is not int or not 0 <= seq < 2 ** 53:
        raise ValueError('Invalid sequence')
    sent = packet.get('sent_ms')
    if type(sent) not in (int, float) or not math.isfinite(sent) or sent < 0:
        raise ValueError('Invalid timestamp')
    if type(packet.get('active')) is not bool or packet.get('source') not in ('gamepad', 'simulation'):
        raise ValueError('Invalid input state')
    if type(packet.get('enabled')) is not bool or not isinstance(packet.get('epoch'), str) or len(packet['epoch']) != 32:
        raise ValueError('Invalid control session')
    if type(packet.get('lease')) is not int or not 0 <= packet['lease'] < 2 ** 53:
        raise ValueError('Invalid freshness lease')
    age = packet.get('video_age_ms')
    if age is not None and (type(age) not in (int, float) or not math.isfinite(age) or age < 0):
        raise ValueError('Invalid video age')
    if packet.get('mapping') not in ('standard', ''):
        raise ValueError('Invalid mapping')
    arrays = {}
    for name, maximum, low in [('axes', 16, -1), ('buttons', 32, 0)]:
        values = packet.get(name)
        if not isinstance(values, list) or len(values) > maximum or any(
                type(x) not in (int, float) or not math.isfinite(x) or not low <= x <= 1 for x in values):
            raise ValueError(f'Invalid {name}')
        arrays[name] = values
    if packet['active'] and packet['mapping'] == 'standard' and (len(arrays['axes']) < 4 or len(arrays['buttons']) < 16):
        raise ValueError('Incomplete standard mapping')
    return {key: packet[key] for key in ('v', 'seq', 'sent_ms', 'active', 'enabled', 'epoch',
                                        'lease', 'source', 'mapping')} | arrays | {'video_age_ms': age}


def driving_preview(packet):
    if not packet['active'] or packet['mapping'] != 'standard':
        return {'steering': 0, 'throttle': 0, 'brake': 0}
    steering = packet['axes'][0]
    steering = math.copysign(max(0, abs(steering) - .1) / .9, steering)
    return {'steering': round(steering, 4), 'throttle': packet['buttons'][7], 'brake': packet['buttons'][6]}


class GamepadReceipt:
    def __init__(self, output, clock=time.monotonic, policy=None):
        self.clock = clock
        self.policy = policy or load_profile()
        self.lock = threading.Lock()
        self.channels = {}
        self.log = JsonLog(output / 'gamepad.jsonl', self.policy['diagnostics'] == 'full',
                           self.policy['log_queue_size'])

    def _lease(self, state, now):
        state['lease_seq'] += 1
        state['leases'] = {key: at for key, at in state['leases'].items()
                           if (now - at) * 1000 < self.policy['input_max_age_ms']}
        state['leases'][state['lease_seq']] = now
        return {'epoch': state['epoch'], 'lease': state['lease_seq']}

    def open(self, consumer, require_video=True):
        with self.lock:
            now = self.clock()
            state = dict(received=0, rejected=0, last_seq=-1, last_log=-1, at=None,
                         packet=None, deadline=0, enabled=False, reason='paused',
                         epoch=secrets.token_hex(16), lease_seq=0, leases={}, last_reply=now,
                         require_video=require_video)
            self.channels[consumer] = state
            return {'type': 'gamepad-lease', **self._lease(state, now)}

    def paused(self):
        with self.lock:
            return all(not state['enabled'] for state in self.channels.values())

    def receive(self, consumer, text, allow_enable=True):
        if not isinstance(text, str) or len(text) > 4096:
            raise ValueError('Gamepad packet exceeds 4096 characters')
        value = json.loads(text)
        now = self.clock()
        with self.lock:
            state = self.channels.get(consumer)
            if state is None:
                raise ValueError('Control session is not open')
            if value == {'type': 'lease-request'}:
                if now - state['last_reply'] < 1 / self.policy['input_receive_limit_hz']:
                    return None
                state['last_reply'] = now
                return {'type': 'gamepad-lease', **self._lease(state, now)}
            packet = validate_packet(text)
            if packet['epoch'] != state['epoch']:
                raise ValueError('Control session has changed')
            # Unordered delivery is intentional; older snapshots must never win.
            if packet['seq'] <= state['last_seq']:
                state['rejected'] += 1
                return None
            deactivated = state['enabled'] and not packet['enabled']
            if state['at'] is not None and now - state['at'] < 1 / self.policy['input_receive_limit_hz'] and not deactivated:
                state['rejected'] += 1
                return None
            issued = state['leases'].get(packet['lease'])
            input_age = (now - issued) * 1000 if issued is not None else None
            timely = input_age is not None and 0 <= input_age < self.policy['input_max_age_ms']
            # Tokens originate before the browser samples input. Their server-clock
            # age bounds input age without subtracting unrelated monotonic clocks.
            if not packet['enabled']:
                state['enabled'] = False
            elif timely and allow_enable:
                state['enabled'] = True
            accepted = timely and (allow_enable or not packet['enabled'])
            video_age = packet['video_age_ms'] if state['require_video'] else 0
            reason = 'input-age' if not timely else 'changing-settings' if not accepted else \
                     'paused' if not state['enabled'] else 'inactive' if not packet['active'] else \
                     'video-unknown' if video_age is None else \
                     'video-age' if state['require_video'] and video_age + input_age >= self.policy['video_max_age_ms'] else 'fresh'
            video_deadline = now + (self.policy['video_max_age_ms'] - video_age - input_age) / 1000 \
                if video_age is not None and state['require_video'] else float('inf')
            state['deadline'] = min(issued + self.policy['input_max_age_ms'] / 1000,
                                    now + self.policy['input_timeout_ms'] / 1000, video_deadline) \
                if reason == 'fresh' else 0
            state['reason'] = reason
            if not accepted:
                state['rejected'] += 1
            state.update(last_seq=packet['seq'], at=now, packet=packet, received=state['received'] + 1)
            state['last_reply'] = now
            ack = {'type': 'gamepad-ack', 'seq': packet['seq'], 'sent_ms': packet['sent_ms'],
                   'received_ms': now * 1000, 'received': state['received'], 'rejected': state['rejected'],
                    'active': reason == 'fresh', 'enabled': state['enabled'], 'accepted': accepted,
                    'reason': reason, 'source': packet['source'],
                    'input_age_upper_bound_ms': round(input_age, 3) if input_age is not None else None,
                    'axes': packet['axes'], 'buttons': packet['buttons'],
                    'preview': driving_preview(packet) if reason == 'fresh' else driving_preview({'active': False}),
                    'actuators_enabled': False, **self._lease(state, now)}
            if now - state['last_log'] >= 1 / self.policy['input_log_rate_hz'] or deactivated:
                self.log.write({'consumer': consumer, **ack})
                state['last_log'] = now
            return ack

    def remove(self, consumer):
        with self.lock:
            self.channels.pop(consumer, None)

    def snapshot(self):
        now = self.clock()
        with self.lock:
            result = {}
            for consumer, state in self.channels.items():
                age = (now - state['at']) * 1000 if state['at'] is not None else None
                fresh = state['enabled'] and now < state['deadline']
                result[consumer] = {'received': state['received'], 'rejected': state['rejected'],
                    'last_seq': state['last_seq'], 'age_ms': age, 'fresh': fresh,
                    'enabled': state['enabled'], 'reason': state['reason'] if fresh or state['reason'] != 'fresh' else 'expired',
                    'source': state['packet']['source'] if state['packet'] else None,
                    'preview': driving_preview(state['packet']) if fresh else {'steering': 0, 'throttle': 0, 'brake': 0},
                    'actuators_enabled': False}
            return result

    def close(self):
        self.log.close()
