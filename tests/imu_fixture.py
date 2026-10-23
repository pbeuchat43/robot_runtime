"""Finite, hardware-free IMU and independent diagnostic providers in domain 225."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import json
import math
import os
from pathlib import Path
import signal
import sys
import time


def make_profile(area, token):
    launch = area / "launch-imu.sh"
    launch.write_text('set -e\nsource /opt/ros/jazzy/setup.bash\nexec /usr/bin/python3 "$@"\n')
    namespace = "/dr_test_" + token
    common = dict(unit_id="idle", node=namespace + "/imu", qos=dict(reliability="best_effort", durability="volatile", depth=1),
                  max_rate_hz=20, stale_after_seconds=.1, max_serialized_bytes=2048, max_json_bytes=4096)
    topics = [dict(**common, id="imu", label="Synthetic IMU orientation and motion", name=namespace + "/imu/data", type="sensor_msgs/msg/Imu",
                   projection=dict(kind="imu", frame_id="sensor", timestamp_clock="system", timestamp_semantics="estimated_mcu_callback_receipt",
                                   orientation_reference="+X east · +Y magnetic north · +Z up", acceleration_semantics="specific_force"))]
    for role, key, unit, progress in (("imu", "imu-health", "idle", None), ("peer", "peer-health", "peer", "frames")):
        topics.append(dict(**dict(common, unit_id=unit, node=namespace + "/" + role, max_rate_hz=5, stale_after_seconds=1.2,
                                  max_serialized_bytes=16384, max_json_bytes=8192),
                           id=key, label="Synthetic " + role + " health", name=namespace + "/" + role + "/diagnostics",
                           type="diagnostic_msgs/msg/DiagnosticArray", projection=dict(kind="diagnostic", status_name=role,
                           keys=["frames", "state", "accuracy"], progress_key=progress)))
    units = []
    for role, unit in (("imu", "idle"), ("peer", "peer")):
        units.append(dict(id=unit, label="Synthetic " + role, argv=["/bin/bash", "--noprofile", "--norc", str(launch), str(Path(__file__).resolve()), str(area), namespace, role],
                          working_directory=str(area), environment=dict(PATH="/usr/bin:/bin", HOME=str(Path.home()), ROS_DOMAIN_ID="225",
                          ROS_AUTOMATIC_DISCOVERY_RANGE="LOCALHOST", ROS_STATIC_PEERS="", ROS_LOG_DIR=str(area / "ros-logs"), PYTHONNOUSERSITE="1"),
                          stop=dict(signal="SIGINT", timeout_seconds=3), public_logs=True))
    return dict(schema_version=1, robot_id="imu-fixture", namespace="i" + token, units=units,
                ros=dict(schema_version=1, python="/usr/bin/python3", setup_scripts=["/opt/ros/jazzy/setup.bash"], domain_id=225,
                         discovery="LOCALHOST", topics=topics, services=[]))


def main():
    import rclpy
    from rclpy.node import Node
    from rclpy.signals import SignalHandlerOptions
    from sensor_msgs.msg import Imu
    from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
    area, namespace, role = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    assert area.parent.parent == Path("/tmp") and area.parent.name.startswith("robot-runtime-ros-")
    assert namespace.startswith("/dr_test_") and os.environ["ROS_DOMAIN_ID"] == "225" and role in ("imu", "peer")
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    node = Node(role, namespace=namespace, enable_rosout=False, start_parameter_services=False)
    health = node.create_publisher(DiagnosticArray, namespace + "/" + role + "/diagnostics", 1)
    shared = node.create_publisher(DiagnosticArray, namespace + "/diagnostics", 1)
    imu = node.create_publisher(Imu, namespace + "/imu/data", 1) if role == "imu" else None
    collision = None
    stopped, counter, frozen_stamp = False, 0, None
    def stop(*_):
        nonlocal stopped
        stopped = True
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
        signal.signal(sig, stop)
    signal.alarm(300)
    with (area / ("provider-events.jsonl" if role == "imu" else "peer-events.jsonl")).open("a") as stream:
        stream.write(json.dumps(dict(event="ready")) + "\n")
    print("ready", flush=True)
    try:
        while not stopped:
            try:
                control = json.loads((area / (role + "-control.json")).read_text())
            except FileNotFoundError:
                control = {}
            if control.get("exit"):
                return 7
            counter += 1
            if imu and control.get("publish", True):
                message = Imu()
                stamp = time.time_ns() + int(control.get("stamp_offset", 0) * 1e9)
                if control.get("freeze_stamp"):
                    frozen_stamp = frozen_stamp or stamp
                    stamp = frozen_stamp
                else:
                    frozen_stamp = None
                message.header.stamp.sec, message.header.stamp.nanosec = divmod(stamp, 10**9)
                message.header.frame_id = control.get("frame", "sensor")
                yaw = math.radians(control.get("yaw", 0))
                message.orientation.z, message.orientation.w = math.sin(yaw/2), math.cos(yaw/2)
                if control.get("invalid_quaternion"):
                    message.orientation.w = message.orientation.z = 0.
                message.angular_velocity.x, message.angular_velocity.y, message.angular_velocity.z = (0.1, -0.2, 0.3)
                message.linear_acceleration.z = 9.81
                for field in control.get("missing", []):
                    getattr(message, field + "_covariance")[0] = -1.
                imu.publish(message)
            item = DiagnosticStatus(name=control.get("name", role), level=bytes([control.get("level", 0)]), message=control.get("state", "READY"),
                values=[KeyValue(key="frames", value=str(counter)), KeyValue(key="state", value=control.get("state", "READY")), KeyValue(key="accuracy", value="high")])
            diagnostic = DiagnosticArray(status=[item])
            if control.get("publish_health", True):
                health.publish(diagnostic)
            # These two intentionally share a topic; the runtime uses the
            # separately declared per-provider topics, never status-name guesses.
            shared.publish(diagnostic)
            if role == "peer":
                if control.get("collide") and collision is None:
                    collision = node.create_publisher(DiagnosticArray, namespace + "/imu/diagnostics", 1)
                if not control.get("collide") and collision is not None:
                    node.destroy_publisher(collision)
                    collision = None
                if collision is not None:
                    collision.publish(DiagnosticArray(status=[DiagnosticStatus(name="imu", level=b"\0", message="wrong provider")]))
            value = dict(health_subscribers=health.get_subscription_count(), shared_subscribers=shared.get_subscription_count(),
                         imu_subscribers=imu.get_subscription_count() if imu else None, frames=counter)
            temporary = area / (role + "-status.tmp")
            temporary.write_text(json.dumps(value))
            os.replace(temporary, area / (role + "-status.json"))
            rclpy.spin_once(node, timeout_sec=.01)
    finally:
        node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
