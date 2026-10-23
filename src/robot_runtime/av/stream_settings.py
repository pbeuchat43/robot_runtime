# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
"""Validated, serialized stream changes; no polling or work between requests."""
import math
import threading
import time


FIELDS = ('width', 'height', 'fps', 'mode', 'bitrate', 'min_bitrate', 'max_bitrate', 'keyframe_interval')


def settings_from_args(args):
    return {key: getattr(args, key) for key in FIELDS}


def settings_options(initial):
    # Preserve an explicitly configured launch mode without advertising other
    # untested sizes/rates. These are output sizes, not native sensor modes.
    return {'resolutions': [list(size) for size in dict.fromkeys(
                [(640, 360), (1280, 720), (initial['width'], initial['height'])])],
            'frame_rates': sorted({5, 10, 15, 30, initial['fps']}),
            'bitrate_min': 50000, 'bitrate_max': max(20000000, initial['max_bitrate']),
            'keyframe_min': .25, 'keyframe_max': 5}


def validate_settings(value, options):
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise ValueError('Send all stream settings and no unknown fields')
    for key in ('width', 'height', 'fps', 'bitrate', 'min_bitrate', 'max_bitrate'):
        if type(value[key]) is not int:
            raise ValueError(f'{key} must be an integer')
    if [value['width'], value['height']] not in options['resolutions']:
        raise ValueError('Choose a listed output resolution')
    if value['fps'] not in options['frame_rates']:
        raise ValueError('Choose a listed frame rate')
    if value['mode'] not in ('adaptive', 'fixed'):
        raise ValueError('Bitrate mode must be adaptive or fixed')
    if not (options['bitrate_min'] <= value['min_bitrate'] <= value['bitrate'] <=
            value['max_bitrate'] <= options['bitrate_max']):
        raise ValueError('Bitrates must satisfy minimum ≤ initial/fixed target ≤ maximum, within the displayed limits')
    interval = value['keyframe_interval']
    if type(interval) not in (int, float) or not math.isfinite(interval) or not (
            options['keyframe_min'] <= interval <= options['keyframe_max']):
        raise ValueError('Keyframe interval must be between 0.25 and 5 seconds')
    return value.copy()


class SettingsConflict(ValueError):
    pass


class StreamSettings:
    """One apply at a time, with rollback and a revision for multiple viewers."""
    def __init__(self, initial, apply, record=lambda event: None):
        self.applied = initial.copy()
        self.options = settings_options(initial)
        self.apply, self.record = apply, record
        self.lock = threading.RLock()
        self.revision = 0
        self.change_id = 0
        self.applying = self.closed = self.failed = False
        self.requested = self.error = self.thread = None
        self.last_applied_ms = time.time() * 1000

    def snapshot(self):
        with self.lock:
            return {'applied': self.applied.copy(), 'requested': self.requested.copy() if self.requested else None,
                    'applying': self.applying, 'revision': self.revision, 'change_id': self.change_id, 'error': self.error,
                    'failed': self.failed, 'last_applied_ms': self.last_applied_ms, 'options': self.options}

    def request(self, body):
        if not isinstance(body, dict) or set(body) != {'revision', 'settings'} or type(body['revision']) is not int:
            raise ValueError('Send settings and their integer revision')
        target = validate_settings(body['settings'], self.options)
        with self.lock:
            if self.closed or self.failed:
                raise SettingsConflict('Stream settings are unavailable; restart the server')
            if self.applying:
                raise SettingsConflict('A stream settings change is already running')
            if body['revision'] != self.revision:
                raise SettingsConflict('Settings changed in another viewer; reload the current settings')
            if target == self.applied:
                return False
            self.requested, self.applying, self.error = target, True, None
            self.change_id += 1
            self.thread = threading.Thread(target=self._worker, args=(target, self.applied.copy()), daemon=True)
            self.thread.start()
            return True

    def _worker(self, target, previous):
        started = time.monotonic()
        error, failed = None, False
        try:
            self.apply(target)
        except Exception as cause:
            error = f'Could not apply settings: {cause}.'
            if not self.closed:
                try:
                    self.apply(previous)
                    error += ' Previous settings restored.'
                except Exception as rollback:
                    error += f' Could not restore the previous stream: {rollback}.'
                    failed = True
        with self.lock:
            if error is None and not self.closed:
                self.applied = target.copy()
                self.revision += 1
                self.last_applied_ms = time.time() * 1000
            self.error, self.failed = error, failed
            self.requested, self.applying = None, False
            event = {'mono_ms': time.monotonic() * 1000, 'duration_ms': (time.monotonic() - started) * 1000,
                     'requested': target, 'previous': previous, 'revision': self.revision,
                     'error': error, 'rollback_failed': failed, 'closed': self.closed}
        self.record(event)

    def close(self):
        with self.lock:
            self.closed = True
            worker = self.thread
        if worker:
            worker.join(timeout=12)
            if worker.is_alive():
                raise RuntimeError('Stream settings worker did not stop')
