"""Finite native ROS spatial/diagnostic fixture in isolated domain 223."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import json
import os
from pathlib import Path
import signal
import shlex
import sys
import time


def make_profile(area, token):
    setup = str(area.parent / "test-overlay/install/local_setup.bash")
    launch = area / "launch-observations.sh"
    launch.write_text('set -e\nsource /opt/ros/jazzy/setup.bash\nsource ' + shlex.quote(setup) + '\nexec /usr/bin/python3 "$@"\n')
    namespace = "/dr_test_" + token
    common = dict(unit_id="idle", node=namespace + "/provider", qos=dict(reliability="best_effort", durability="volatile", depth=1),
                  max_rate_hz=10, stale_after_seconds=0.6, max_serialized_bytes=16384, max_json_bytes=8192)
    topics = [dict(**common, id="cones", label="Synthetic cones", name=namespace + "/cones", type="robot_runtime_test_interfaces/msg/ConeDetections",
                   projection=dict(kind="detections", array_field="detections", position_field="position", color_field="color",
                                   confidence_field="classification_confidence", latency_field="acquisition_to_publish_latency_s",
                                   frame_id="base_link", max_items=20, forward_m=6, half_width_m=3, acquisition_clock="system")),
              dict(**dict(common, stale_after_seconds=1.2), id="health", label="Synthetic camera health", name=namespace + "/health",
                   type="diagnostic_msgs/msg/DiagnosticArray", projection=dict(kind="diagnostic", status_name="Fixture camera",
                   keys=["frames", "state"], progress_key="frames"))]
    config = dict(schema_version=1, robot_id="observation-fixture", namespace="o" + token, units=[
        dict(id="idle", label="Synthetic observation provider", argv=["/bin/bash", "--noprofile", "--norc", str(launch), str(Path(__file__).resolve()), str(area), namespace],
             working_directory=str(area), environment=dict(PATH="/usr/bin:/bin", HOME=str(Path.home()), ROS_DOMAIN_ID="223",
             ROS_AUTOMATIC_DISCOVERY_RANGE="LOCALHOST", ROS_STATIC_PEERS="", ROS_LOG_DIR=str(area / "ros-logs"), PYTHONNOUSERSITE="1"),
             stop=dict(signal="SIGINT", timeout_seconds=3), public_logs=True)],
        ros=dict(schema_version=1, python="/usr/bin/python3", setup_scripts=["/opt/ros/jazzy/setup.bash", setup], domain_id=223,
                 discovery="LOCALHOST", topics=topics, services=[]))
    (area / "profile.json").write_text(json.dumps(config))
    return config


def main():
    import rclpy
    from rclpy.node import Node
    from rclpy.signals import SignalHandlerOptions
    from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
    from robot_runtime_test_interfaces.msg import ConeDetections, ConeDetection
    area, namespace = Path(sys.argv[1]), sys.argv[2]
    assert area.parent.parent == Path("/tmp") and area.parent.name.startswith("robot-runtime-ros-")
    assert namespace.startswith("/dr_test_") and os.environ["ROS_DOMAIN_ID"] == "223"
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    node = Node("provider", namespace=namespace, enable_rosout=False, start_parameter_services=False)
    cones = node.create_publisher(ConeDetections, namespace + "/cones", 1)
    health = node.create_publisher(DiagnosticArray, namespace + "/health", 1)
    stopped, counter, frozen_stamp = False, 0, None
    def stop(*_):
        nonlocal stopped
        stopped = True
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
        signal.signal(sig, stop)
    signal.alarm(300)
    with (area / "provider-events.jsonl").open("a") as events:
        events.write(json.dumps(dict(event="ready")) + "\n")
    print("ready", flush=True)
    try:
        while not stopped:
            try:
                control = json.loads((area / "control.json").read_text())
            except FileNotFoundError:
                control = {}
            if not control.get("freeze_health"):
                counter += 1
            if control.get("publish", True):
                message = ConeDetections()
                stamp = time.time_ns() + int(control.get("stamp_offset", 0) * 1e9)
                if control.get("freeze_stamp"):
                    frozen_stamp = frozen_stamp or stamp
                    stamp = frozen_stamp
                else:
                    frozen_stamp = None
                message.header.stamp.sec, message.header.stamp.nanosec = divmod(stamp, 10**9)
                message.header.frame_id = control.get("frame", "base_link")
                message.acquisition_to_publish_latency_s = 0.01
                for index in range(control.get("count", 2)):
                    item = ConeDetection()
                    item.position.x, item.position.y, item.position.z = (1.0, 1.0, 0.2) if index % 2 == 0 else (2.0, -1.0, 0.3)
                    item.color, item.classification_confidence = index % 3, 0.8
                    message.detections.append(item)
                cones.publish(message)
            if control.get("publish_health", True):
                item = DiagnosticStatus(name="Fixture camera", level=bytes([control.get("level", 0)]), message="Synthetic camera",
                    values=[KeyValue(key="frames", value=str(counter)), KeyValue(key="state", value="fixture")])
                diagnostic = DiagnosticArray(status=[item] * (2 if control.get("duplicate_status") else 1))
                health.publish(diagnostic)
            status = dict(cone_subscribers=cones.get_subscription_count(), health_subscribers=health.get_subscription_count(), frames=counter)
            temporary = area / "provider-status.tmp"
            temporary.write_text(json.dumps(status))
            os.replace(temporary, area / "provider-status.json")
            rclpy.spin_once(node, timeout_sec=0.05)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
