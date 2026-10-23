from pathlib import Path
import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import subprocess
import sys
import tarfile
import zipfile
import robot_runtime

stage = Path(__file__).resolve().parent
source = stage / 'source'
package = Path(robot_runtime.__file__).resolve().parent
assert not package.is_relative_to(source)
assert 'PYTHONPATH' not in os.environ
assert metadata.version('robot-runtime') == robot_runtime.__version__ == '0.1.0.dev19'
entries = {e.name: e.value for e in metadata.distribution('robot-runtime').entry_points}
assert set(entries) == {'robot-runtime', 'robot-runtime-manager', 'robot-runtime-web', 'robot-runtime-av'}
for name in entries:
    subprocess.run([str(Path(sys.executable).parent / name), '--help'], check=True, capture_output=True, timeout=10)
files = {}
for path in sorted((source / 'src/robot_runtime').rglob('*')):
    if not path.is_file() or '__pycache__' in path.parts:
        continue
    relative = path.relative_to(source / 'src/robot_runtime')
    assert (package / relative).read_bytes() == path.read_bytes(), relative
    files[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
for name in ('NOTICE.txt', 'LICENSE-MPL-2.0', 'LICENSE-webrtc-adapter'):
    assert (package / 'static/av/vendor' / name).is_file()
wheel = stage / 'dist/robot_runtime-0.1.0.dev19-py3-none-any.whl'
sdist, = (stage / 'dist').glob('*.tar.gz')
with tarfile.open(sdist) as archive:
    names = archive.getnames()
    for suffix in ('src/robot_runtime/__init__.py', 'deployment/action_demo_interfaces/action/Count.action',
                   'tests/interfaces/msg/ConeDetections.msg', 'tests/integration_browser.py', 'docs/integration-contract.md'):
        assert any(n.endswith('/' + suffix) for n in names), suffix
report = dict(passed=True, python=sys.version, platform=platform.platform(), version=robot_runtime.__version__,
              entry_points=entries, package_sha256=files,
              artifacts={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (wheel, sdist)},
              dependencies={n: metadata.version(n) for n in ('aiohttp', 'dbus-python', 'PyGObject')})
(stage / 'package-identity.json').write_text(json.dumps(report, indent=2))
print(json.dumps(dict(passed=True, version=report['version'], package_files=len(files), entry_points=list(entries))))
