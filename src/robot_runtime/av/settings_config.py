# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
"""Validated launch profiles. JSON defaults < user JSON < explicit CLI options."""
import json
import math
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).with_name('profiles.json')
INTEGER_FIELDS = {'width', 'height', 'fps', 'bitrate', 'min_bitrate', 'max_bitrate',
                  'telemetry_interval_ms', 'host_log_interval_ms', 'clock_sync_interval_ms',
                  'clock_probe_count', 'clock_valid_ms', 'health_check_interval_ms',
                  'placeholder_interval_ms', 'log_queue_size'}
NUMBER_FIELDS = {'keyframe_interval', 'idle_release_s', 'input_max_age_ms', 'input_timeout_ms',
                 'input_rate_hz', 'input_receive_limit_hz', 'input_log_rate_hz',
                 'clock_max_uncertainty_ms', 'camera_stall_timeout_s', 'camera_retry_interval_s',
                 'discovery_timeout_s', 'ros_clock_tolerance_ms', 'max_source_age_ms',
                 'video_max_age_ms', 'video_stall_ms'}
OTHER_FIELDS = {'mode', 'diagnostics', 'source', 'stamp', 'ros_topic', 'ros_domain_id',
                'ros_use_sim_time', 'capture_mode', 'native_format_preference'}
FIELDS = INTEGER_FIELDS | NUMBER_FIELDS | OTHER_FIELDS


def load_profile(profile='teleop', path=None, overrides=None):
    values = json.loads(DEFAULT_CONFIG.read_text())
    if profile not in values['profiles']:
        raise ValueError('Choose teleop or observer')
    result = values['common'] | values['profiles'][profile]
    if path:
        custom = json.loads(Path(path).read_text())
        if not isinstance(custom, dict) or set(custom) - {'common', 'profiles'}:
            raise ValueError('Configuration accepts common and profiles objects only')
        common = custom.get('common', {})
        profiles = custom.get('profiles', {})
        if not isinstance(common, dict) or not isinstance(profiles, dict) or set(profiles) - {'teleop', 'observer'}:
            raise ValueError('Invalid configuration sections')
        for part in [common, *profiles.values()]:
            if not isinstance(part, dict) or set(part) - FIELDS:
                raise ValueError('Unknown profile setting')
        result.update(common)
        result.update(profiles.get(profile, {}))
    if overrides:
        if set(overrides) - FIELDS:
            raise ValueError('Unknown launch override')
        result.update({key: value for key, value in overrides.items() if value is not None})
    for name in INTEGER_FIELDS | NUMBER_FIELDS:
        value = result[name]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f'{name} must be a finite nonnegative number')
        if name in INTEGER_FIELDS and type(value) is not int:
            raise ValueError(f'{name} must be an integer')
        if value == 0 and name not in {'idle_release_s', 'ros_clock_tolerance_ms'}:
            raise ValueError(f'{name} must be positive')
    if not 1 <= result['fps'] <= 60 or not 1 <= result['input_rate_hz'] <= result['input_receive_limit_hz'] <= 200:
        raise ValueError('Use FPS 1–60 and input rate ≤ receive limit ≤ 200 Hz')
    if result['width'] < 192 or result['height'] < 48 or result['width'] % 2 or result['height'] % 2:
        raise ValueError('Use even output dimensions of at least 192×48')
    if not 50000 <= result['min_bitrate'] <= result['bitrate'] <= result['max_bitrate']:
        raise ValueError('Bitrates must satisfy 50000 ≤ minimum ≤ initial ≤ maximum')
    if not .25 <= result['keyframe_interval'] <= 5:
        raise ValueError('Keyframe interval must be 0.25–5 seconds')
    if result['mode'] not in ('adaptive', 'fixed') or result['diagnostics'] not in ('minimal', 'full'):
        raise ValueError('Invalid bitrate mode or diagnostics level')
    if type(result['stamp']) is not bool or type(result['ros_use_sim_time']) is not bool:
        raise ValueError('stamp and ros_use_sim_time must be booleans')
    if profile == 'teleop' and not result['stamp']:
        raise ValueError('Teleop needs frame timestamps to assess image freshness')
    if not isinstance(result['source'], str) or not result['source']:
        raise ValueError('source must be a nonempty string')
    if not isinstance(result['ros_topic'], str) or not result['ros_topic'].startswith('/'):
        raise ValueError('ros_topic must be an absolute topic name')
    domain = result['ros_domain_id']
    if domain is not None and (type(domain) is not int or not 0 <= domain <= 232):
        raise ValueError('ros_domain_id must be null or an integer from 0 to 232')
    if result['capture_mode'] is not None and not isinstance(result['capture_mode'], str):
        raise ValueError('capture_mode must be null or FOURCC:WIDTHxHEIGHT@FPS')
    formats = result['native_format_preference']
    if not isinstance(formats, list) or not formats or any(value not in
            ('NV12', 'YUYV', 'UYVY', 'RGB3', 'BGR3', 'MJPG', 'JPEG') for value in formats):
        raise ValueError('Invalid native_format_preference')
    if result['clock_valid_ms'] < result['clock_sync_interval_ms']:
        raise ValueError('clock_valid_ms must cover the clock sync interval')
    if result['clock_probe_count'] > 50 or result['log_queue_size'] > 10000:
        raise ValueError('Use at most 50 clock probes and 10000 queued log records')
    return result


def add_policy_arguments(parser):
    parser.add_argument('--profile', choices=['teleop', 'observer'], default='teleop')
    parser.add_argument('--config', help='JSON overrides; see profiles.json for defaults')
    parser.add_argument('--diagnostics', choices=['minimal', 'full'])
    for name in ('input_max_age_ms', 'input_timeout_ms', 'video_max_age_ms', 'video_stall_ms', 'idle_release_s'):
        parser.add_argument('--' + name.replace('_', '-'), type=float)
    parser.add_argument('--ros-topic')
    parser.add_argument('--ros-domain-id', type=int)
    parser.add_argument('--ros-use-sim-time', action='store_true', default=None)
    parser.add_argument('--capture-mode', help='Explicit native V4L2 mode, e.g. YUYV:640x360@15')


def resolve_arguments(args):
    values = load_profile(args.profile, args.config,
                          {key: getattr(args, key) for key in FIELDS if hasattr(args, key)})
    for key, value in values.items():
        setattr(args, key, value)
    return args
