#!/bin/bash
set -euo pipefail
umask 077
stage=/tmp/robot-runtime-clean-e7b45109c263
slice=/tmp/robot-runtime-slice-e7b45109c263
ros=/tmp/robot-runtime-ros-e7b45109c263
py=$stage/venv/bin/python
unset PYTHONPATH
export PYTHONNOUSERSITE=1
cd "$stage"
run() {
  name=$1
  shift
  printf '%s START %s\n' "$(date -u +%FT%TZ)" "$name" | tee -a progress.log
  if "$@" > "$stage/$name.log" 2>&1; then
    printf '%s PASS %s\n' "$(date -u +%FT%TZ)" "$name" | tee -a progress.log
  else
    result=$?
    printf '%s FAIL %s (%s)\n' "$(date -u +%FT%TZ)" "$name" "$result" | tee -a progress.log
    tail -35 "$stage/$name.log"
    exit "$result"
  fi
}
run package "$py" "$stage/verify-artifact.py"
run compile "$py" -m compileall -q "$stage/source/src" "$stage/source/tests" "$stage/source/deployment"
run unit "$py" -m unittest discover -s "$stage/source/tests" -p 'test_*.py' -v
run runtime "$py" "$stage/source/tests/integration_runtime.py" "$slice"
run clients "$py" "$stage/source/tests/integration_clients.py" "$slice"
run ros "$py" "$stage/source/tests/integration_ros.py" "$ros"
run ros-browser "$py" "$stage/source/tests/integration_browser.py" "$ros" --ros --resources --delivery
run commands "$py" "$stage/source/tests/integration_commands.py" "$ros"
run parameters "$py" "$stage/source/tests/integration_parameters.py" "$ros"
run snapshots "$py" "$stage/source/tests/integration_browser.py" "$ros" --snapshots
run action-build bash --noprofile --norc -c 'source /opt/ros/jazzy/setup.bash; colcon --log-base "$1/action-overlay/log" build --base-paths "$2/deployment/action_demo_interfaces" --build-base "$1/action-overlay/build" --install-base "$1/action-overlay/install" --cmake-args -DPython3_EXECUTABLE=/usr/bin/python3' build-actions "$ros" "$stage/source"
run actions "$py" "$stage/source/tests/integration_actions.py" "$ros"
run actions-browser "$py" "$stage/source/tests/integration_browser.py" "$ros" --actions
run test-interface-build bash --noprofile --norc -c 'source /opt/ros/jazzy/setup.bash; colcon --log-base "$1/test-overlay/log" build --base-paths "$2/tests/interfaces" --build-base "$1/test-overlay/build" --install-base "$1/test-overlay/install" --cmake-args -DPython3_EXECUTABLE=/usr/bin/python3' build-tests "$ros" "$stage/source"
run observations "$py" "$stage/source/tests/integration_browser.py" "$ros" --observations
run lidar "$py" "$stage/source/tests/integration_browser.py" "$ros" --lidar
run fiducials "$py" "$stage/source/tests/integration_browser.py" "$ros" --fiducials
run av-browser "$py" "$stage/source/tests/integration_browser.py" "$slice" --av-native "$stage/native"
printf '%s COMPLETE\n' "$(date -u +%FT%TZ)" | tee -a progress.log
