"""Copy explicitly supplied, pinned native artifacts to a new independent prefix.

Run on the target host. Does not install services, open devices or modify sources.
The runtime input is an existing validated gst-plugin-webrtc 0.14.5 build with a
target/release library and the GStreamer 1.24.2 PyGObject/libnice sysroot.
"""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--system-root', type=Path,
                        help='Installed package root, normally /; defaults to the historical build sysroot')
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12) or os.uname().machine != 'aarch64':
        parser.error('This artifact preparation is for CPython 3.12 on aarch64')
    root = args.destination.resolve()
    if root.exists():
        parser.error('Choose a new destination; existing native prefixes are immutable')
    runtime = args.runtime.resolve()
    system_root = args.system_root.resolve() if args.system_root else runtime / 'sysroot'
    pairs = [(runtime / 'target/release/libgstrswebrtc.so', root / 'gstreamer/libgstrswebrtc.so'),
             (runtime / 'target/release/libgstrsrtp.so', root / 'gstreamer/libgstrsrtp.so'),
             (system_root / 'usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstnice.so', root / 'gstreamer/libgstnice.so')]
    pairs.append((Path(__file__).resolve().parents[1] / 'src/robot_runtime/static/av/vendor/LICENSE-MPL-2.0',
                  root / 'licenses/gst-plugins-rs-MPL-2.0'))
    overrides = system_root / 'usr/lib/python3/dist-packages/gi/overrides'
    pairs += [(source, root / 'gi' / source.name) for source in overrides.iterdir() if source.is_file()]
    for name in ('python3-gst-1.0', 'gstreamer1.0-nice'):
        pairs.append((system_root / 'usr/share/doc' / name / 'copyright', root / 'licenses' / name))
    for source, _destination in pairs:
        if not source.is_file():
            parser.error('Missing native artifact: ' + str(source))
    os.umask(0o077)
    root.mkdir(mode=0o700)
    manifest = dict(platform='aarch64-cpython312', expected_webrtc_version='0.14.5', files=[])
    for source, destination in pairs:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        manifest['files'].append(dict(path=str(destination.relative_to(root)),
            source=str(source.resolve()), sha256=hashlib.sha256(destination.read_bytes()).hexdigest()))
    (root / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(dict(prefix=str(root), files=len(pairs), manifest=str(root / 'manifest.json'))))


if __name__ == '__main__':
    main()
