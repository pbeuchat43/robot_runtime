"""Explicit native AV units for the isolated browser harness; synthetic source."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import json
import sys
from pathlib import Path


def configure(profile, area, native):
    native = Path(native).resolve()
    manifest = json.loads((native / 'manifest.json').read_text())
    assert manifest['platform'] == 'aarch64-cpython312'
    profile['av'] = dict(services=[])
    for kind, port in [('media', 41200), ('input', 41210)]:
        identity = 'video' if kind == 'media' else 'input'
        environment = dict(GST_PLUGIN_PATH=str(native / 'gstreamer'), GST_REGISTRY=str(area / (identity + '-registry.bin')),
                           ROBOT_AV_GI_OVERRIDES=str(native / 'gi'), PYTHONPATH=str(native / 'python'))
        profile['units'].append(dict(id=identity, label='Synthetic video' if kind == 'media' else 'Diagnostic input',
            argv=[sys.executable, '-m', 'robot_runtime.av.worker', '--profile', str(area / 'profile.json'),
                  '--state', str(area / 'state'), '--capability', identity], working_directory=str(area),
            environment=environment, stop=dict(signal='SIGINT', timeout_seconds=10), public_logs=False))
        service = dict(id=identity, label=profile['units'][-1]['label'], kind=kind, unit_id=identity,
                       host='127.0.0.1', udp_port=port, max_sessions=5)
        if kind == 'media':
            service.update(source=dict(kind='test', label='Synthetic moving pattern'), encoder='hardware')
        profile['av']['services'].append(service)
    return profile
