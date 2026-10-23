"""Isolated real Foxglove bridge and finite harmless ROS endpoints for integration."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import json
import os
from pathlib import Path
import signal
import sys
import time

import yaml


def make_profile(area, token):
    from integration_browser import port
    prepared = area.parent / "foxglove-export"
    source = json.loads((prepared / "profile.json").read_text())
    bridge_port = port()
    parameters = yaml.safe_load((prepared / "parameters.yaml").read_text())
    parameters["/foxglove_bridge"]["ros__parameters"].update(address="127.0.0.1", port=bridge_port)
    (area / "parameters.yaml").write_text(yaml.safe_dump(parameters))
    (area / "launch.sh").write_text('set -e\nsource /opt/ros/jazzy/setup.bash\nexec "$@"\n')
    unit = next(unit for unit in source["units"] if unit["id"] == "foxglove_bridge")
    unit.update(id="idle", label="Isolated Foxglove bridge", working_directory=str(area),
        argv=["/bin/bash", "--noprofile", "--norc", str(area / "launch.sh"),
              "/opt/ros/jazzy/lib/foxglove_bridge/foxglove_bridge", "--ros-args", "-r", "__node:=foxglove_bridge", "--params-file", str(area / "parameters.yaml")])
    unit["environment"].update(ROS_DOMAIN_ID="230", ROS_LOG_DIR=str(area / "ros-logs"))
    settings = next(spec for spec in source["ros"]["parameters"] if spec["id"] == "foxglove-settings")
    settings["unit_id"] = "idle"
    profile = dict(schema_version=1, robot_id="foxglove-fixture", namespace="f" + token, units=[unit],
        ros=dict(schema_version=1, python="/usr/bin/python3", setup_scripts=["/opt/ros/jazzy/setup.bash"],
                 domain_id=230, discovery="LOCALHOST", topics=[], services=[], parameters=[settings]))
    (area / "profile.json").write_text(json.dumps(profile))
    (area / "bridge-port.txt").write_text(str(bridge_port))
    return profile


def main():
    import rclpy
    from rclpy.node import Node
    from rclpy.signals import SignalHandlerOptions
    from std_msgs.msg import String
    from std_srvs.srv import SetBool
    area, namespace = Path(sys.argv[1]).resolve(), sys.argv[2]
    assert area.parent.parent == Path("/tmp") and area.parent.name.startswith("robot-runtime-ros-")
    assert namespace.startswith("/dr_fox_")
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    node = Node("fixture", namespace=namespace)
    node.declare_parameter("gain", 1.0)
    received, calls, stopped = [], [], False
    publisher = node.create_publisher(String, namespace + "/output", 1)
    subscription = node.create_subscription(String, namespace + "/input", lambda message: received.append(message.data), 1)
    def service(request, response):
        calls.append(request.data)
        response.success, response.message = request.data, "Harmless Foxglove test"
        return response
    node.create_service(SetBool, namespace + "/enable", service)
    def stop(*_):
        nonlocal stopped
        stopped = True
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM): signal.signal(number, stop)
    signal.alarm(180)
    next_publish = 0
    try:
        while not stopped:
            if time.monotonic() >= next_publish:
                publisher.publish(String(data="foxglove fixture"))
                temporary = area / "provider-status.tmp"
                temporary.write_text(json.dumps(dict(received=received[-10:], calls=calls[-10:], gain=node.get_parameter("gain").value,
                    input_publishers=node.count_publishers(namespace + "/input"))))
                os.replace(temporary, area / "provider-status.json")
                next_publish = time.monotonic() + .1
            rclpy.spin_once(node, timeout_sec=.02)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
