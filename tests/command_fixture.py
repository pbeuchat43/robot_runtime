"""Finite, hardware-free UInt8 command receiver in an isolated ROS domain."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import argparse
import json
import os
from pathlib import Path
import time


def make_profile(area, token):
    launch = area / "launch-command.sh"
    launch.write_text('set -e\nsource /opt/ros/jazzy/setup.bash\nexec /usr/bin/python3 "$@"\n')
    namespace = "/dr_command_" + token
    environment = dict(PATH="/usr/bin:/bin", HOME=str(Path.home()), ROS_DOMAIN_ID="228", ROS_AUTOMATIC_DISCOVERY_RANGE="LOCALHOST",
                       ROS_STATIC_PEERS="", ROS_LOG_DIR=str(area / "provider-logs"), PYTHONNOUSERSITE="1", RCUTILS_LOGGING_USE_STDOUT="0")
    common = dict(unit_id="idle", node=namespace + "/provider")
    profile = dict(schema_version=1, robot_id="command-fixture", namespace="c" + token, units=[dict(id="idle", label="Synthetic command provider",
        argv=["/bin/bash", "--noprofile", "--norc", str(launch), str(Path(__file__).resolve()), "--area", str(area), "--namespace", namespace],
        working_directory=str(area), environment=environment, stop=dict(signal="SIGINT", timeout_seconds=6), public_logs=True)],
        ros=dict(schema_version=1, python="/usr/bin/python3", setup_scripts=["/opt/ros/jazzy/setup.bash"], domain_id=228, discovery="LOCALHOST",
            services=[], topics=[dict(**common, id="state", label="Observed state", name=namespace + "/state", type="std_msgs/msg/UInt8", fields=["data"],
                qos=dict(reliability="best_effort", durability="volatile", depth=1), max_rate_hz=5, stale_after_seconds=1,
                max_serialized_bytes=1024, max_json_bytes=1024)],
            commands=[dict(**common, id="request", label="State request", name=namespace + "/request", type="std_msgs/msg/UInt8", fields=["data"],
                timeout_seconds=2, choices=[dict(id=key, label=key.title(), arguments=dict(data=value))
                    for key, value in (("disable", 0), ("enable", 1), ("disarm", 4))])]))
    (area / "profile.json").write_text(json.dumps(profile))
    return profile


def run(area, namespace, duplicate):
    import rclpy
    from std_msgs.msg import UInt8
    rclpy.init()
    node = rclpy.create_node("provider", namespace=namespace)
    output = node.create_publisher(UInt8, namespace + "/state", 1)
    state, received = 0, []
    def control():
        path = area / "control.json"
        return json.loads(path.read_text()) if path.exists() else {}
    def accept(message):
        nonlocal state
        received.append(message.data)
        if not control().get("ignore"):
            state = message.data
    node.create_subscription(UInt8, namespace + "/request", accept, 1)
    def publish():
        if not control().get("pause"):
            output.publish(UInt8(data=state))
        if not duplicate:
            temporary = area / "provider-status.tmp"
            temporary.write_text(json.dumps(dict(received=received, state=state, command_publishers=node.count_publishers(namespace + "/request"))))
            os.replace(temporary, area / "provider-status.json")
    node.create_timer(.1, publish)
    print("ready", flush=True)
    if not duplicate:
        with (area / "provider-events.jsonl").open("a") as events:
            events.write(json.dumps(dict(event="ready")) + "\n")
    deadline = time.monotonic() + 300
    try:
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=.1)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok(): rclpy.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--area", type=Path, required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--duplicate", action="store_true")
    args = parser.parse_args()
    run(args.area, args.namespace, args.duplicate)
