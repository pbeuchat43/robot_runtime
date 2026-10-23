"""Isolated profile for the finite action demo; expects its built ROS overlay."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import json
from pathlib import Path


def make_profile(area, token):
    source = Path(__file__).resolve().parent.parent
    overlay = area.parent / "action-overlay/install/local_setup.bash"
    if not overlay.is_file():
        raise RuntimeError("Build deployment/action_demo_interfaces into ROOT/action-overlay first")
    launch = area / "launch-action.sh"
    launch.write_text('set -e\nsource /opt/ros/jazzy/setup.bash\nsource "' + str(overlay) + '"\nexec /usr/bin/python3 "$@"\n')
    namespace = "/dr_action_" + token
    environment = dict(PATH="/usr/bin:/bin", HOME=str(Path.home()), ROS_DOMAIN_ID="227", ROS_AUTOMATIC_DISCOVERY_RANGE="LOCALHOST",
                       ROS_STATIC_PEERS="", ROS_LOG_DIR=str(area / "provider-logs"), PYTHONNOUSERSITE="1", RCUTILS_LOGGING_USE_STDOUT="0")
    config = dict(schema_version=1, robot_id="action-fixture", namespace="a" + token, units=[dict(id="idle", label="Synthetic counting action",
        argv=["/bin/bash", "--noprofile", "--norc", str(launch), str(source / "deployment/action_demo.py"),
              "--area", str(area), "--namespace", namespace, "--test-controls", "--lifetime", "600"],
        working_directory=str(area), environment=environment, stop=dict(signal="SIGINT", timeout_seconds=6), public_logs=True)])
    config["ros"] = dict(schema_version=1, python="/usr/bin/python3", setup_scripts=["/opt/ros/jazzy/setup.bash", str(overlay)],
        domain_id=227, discovery="LOCALHOST", topics=[], services=[], actions=[dict(id="count", label="Count without hardware", unit_id="idle",
        node=namespace + "/provider", name=namespace + "/count", type="robot_runtime_demo_interfaces/action/Count",
        goal_fields=["steps", "period_seconds", "abort_at_end"], result_fields=["success", "completed", "message"],
        feedback_fields=["completed", "total", "message"], response_timeout_seconds=1.5, max_feedback_rate_hz=2, stale_after_seconds=2)])
    (area / "profile.json").write_text(json.dumps(config))
    return config
