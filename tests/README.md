# Runtime verification

All execution checks run through `ssh jah`. Reserve/coordinate the host first. Use new private `/tmp/robot-runtime-slice-<12 lowercase hex>` and `/tmp/robot-runtime-ros-<12 lowercase hex>` directories, an isolated venv with system site packages, and pinned web dependencies. Preserve existing workloads, configuration, hardware, services, and power settings. These suites use synthetic providers; they do not install boot services or operate vehicle hardware.

Transfer source and tests, build/install the candidate wheel, unset `PYTHONPATH`, and execute from outside the source tree to verify installed workers and assets. Distribution D-Bus/GLib bindings, prepared ROS Jazzy, OpenSSL, and `/snap/bin/chromium` are host prerequisites. Compilation and unit discovery may also be run against source during development.

```sh
python -m compileall -q SOURCE/src SOURCE/tests SOURCE/deployment
python -m unittest discover -s SOURCE/tests -p 'test_*.py' -v
python SOURCE/tests/integration_runtime.py SLICE_ROOT
python SOURCE/tests/integration_browser.py SLICE_ROOT
```

Replace `SOURCE`, `SLICE_ROOT`, and `ROS_ROOT` with absolute paths. The native suites use unique namespaces, finite workloads, and loopback traffic. Run suites sequentially because some reuse ROS domains. Browser fixtures create their own certificate, credential, private Chromium profile, and transient service; they do not use personal browser profiles. Reports omit private credentials and TLS keys.

Inspect `passed`, each check list, `cleanup_errors`, and final stopped/empty workload state. Preserve failures alongside successful reruns. Do not infer cleanup merely from a harness exit. Public evidence may contain reports, screenshots, hashes, and dependency identity; credentials, TLS keys, SQLite state, and private backups remain outside tracked evidence. Never vacuum the host journal.

## Shared browser client foundation

```sh
python SOURCE/tests/integration_clients.py SLICE_ROOT
python SOURCE/tests/integration_browser.py ROS_ROOT --ros --resources --delivery
```

Injected clocks/transports cover inert construction, acquisition/release, stale/reconnect handling, replaced callbacks, cancellation, and uncertain commands without replay. Real Chromium EventSource/fetch exercises packaged modules and cleanup. The delivery extension checks three actual tabs, shared channels, bounded reads, authorization, reconnect, and demand release. It does not establish real background-tab suspension or remote latency.

## Native ROS topics, services, parameters, and commands

```sh
python SOURCE/tests/integration_ros.py ROS_ROOT
python SOURCE/tests/integration_parameters.py ROS_ROOT
python SOURCE/tests/integration_commands.py ROS_ROOT
python SOURCE/tests/integration_browser.py ROS_ROOT --ros
python SOURCE/tests/integration_browser.py ROS_ROOT --parameters
python SOURCE/tests/integration_browser.py ROS_ROOT --commands
```

Fixtures cover typed/lossless values, shared demand, stale/empty/byte bounds, provider refusal, duplicate graph identities, exact run/generation rejection, atomic parameters, readback uncertainty, fixed-choice publication, crash recovery, and no replay. ROS topics/services use localhost domain 221, parameters 222, and fixed commands 228. These finite fixtures publish no vehicle commands.

## ROS actions

Build the finite counting interface in the isolated root:

```sh
source /opt/ros/jazzy/setup.bash
colcon --log-base ROS_ROOT/action-overlay/log build \
  --base-paths SOURCE/deployment/action_demo_interfaces \
  --build-base ROS_ROOT/action-overlay/build --install-base ROS_ROOT/action-overlay/install \
  --cmake-args -DPython3_EXECUTABLE=/usr/bin/python3
python SOURCE/tests/integration_actions.py ROS_ROOT
python SOURCE/tests/integration_browser.py ROS_ROOT --actions
```

The domain-227 provider covers acceptance, refusal, feedback, success/abort, exact cancellation, delayed replies, duplicate providers, and UUID recovery without replay. It never controls hardware. Late acceptance may move an uncertain result between timeout states without releasing its reservation prematurely.

## Spatial observations

Build this repository's synthetic messages; no robot deployment overlay is needed:

```sh
source /opt/ros/jazzy/setup.bash
colcon --log-base ROS_ROOT/test-overlay/log build \
  --base-paths SOURCE/tests/interfaces \
  --build-base ROS_ROOT/test-overlay/build --install-base ROS_ROOT/test-overlay/install \
  --cmake-args -DPython3_EXECUTABLE=/usr/bin/python3
python SOURCE/tests/integration_browser.py ROS_ROOT --observations
python SOURCE/tests/integration_browser.py ROS_ROOT --imu
python SOURCE/tests/integration_browser.py ROS_ROOT --lidar
python SOURCE/tests/integration_browser.py ROS_ROOT --fiducials
python SOURCE/tests/integration_browser.py ROS_ROOT --snapshots
```

`robot_runtime_test_interfaces` exists only for synthetic tests. It is not a proposed public perception message package. Observation tests cover full XYZ, timestamp strings, empty replacement, item limits, frames, invalid/future/old stamps, shared demand, and diagnostic progress. Lidar and fiducial tests cover transforms, invalid geometry, stale independent layers, and 2D/3D rendering. IMU and snapshot tests use standard ROS interfaces. No camera, lidar, or IMU is opened.

## Resource monitoring

```sh
python SOURCE/tests/integration_browser.py SLICE_ROOT --resources
python SOURCE/tests/measure_runtime.py SLICE_ROOT --resources --seconds 60
```

Functional checks cover closed-panel idleness, shared history, last-viewer release, gaps, manager restart, and CLI access without web. The native GPU monitor requires the host's `tegrastats`. The separate bounded capture uses finite idle workloads and synthetic SSE consumers; it is not five rendered remote browsers or hardware-load acceptance. Interpret summed RSS and shared-daemon CPU carefully. Discuss difficult numerical targets before extending measurements or optimization.

## Camera and diagnostic gamepad

```sh
python -m unittest discover -s SOURCE/tests -p 'test_av*.py' -v
python SOURCE/tests/integration_browser.py SLICE_ROOT --av-native PREPARED_NATIVE_PREFIX
```

The unit suite needs no camera SDK or GStreamer. The native suite uses synthetic video and controller packets with an explicitly prepared dependency prefix; `deployment/prepare-av-native.py` records copied dependency identities and notices. Checks cover sessions, admission, freshness, authenticated signaling, revocation, and cleanup. They do not establish physical controller input, camera support, vehicle authority, or driving acceptance.

## Optional external bridge

`integration_foxglove.py ROS_ROOT` and `integration_browser.py ROS_ROOT --foxglove` exercise a separately provisioned Foxglove bridge. Its own access policy is distinct from runtime command authentication. It is not required for ordinary runtime operation.

## Packaging and scope

Build both wheel and source distribution. Install the wheel into a fresh isolated venv; check the four CLI entry points and compare installed package assets against source, including third-party notices. Run applicable native/browser checks with `PYTHONPATH` unset. Read the [current verification record](../docs/verification.md) for actual evidence and limits. Historical deployment-specific installers, hardware pilots, and their reports are outside this standalone test suite.
