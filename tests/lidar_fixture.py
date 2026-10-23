"""Finite, separate cone and lidar providers in isolated ROS domain 226."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import copy
import json
import math
import os
from pathlib import Path
import signal
import sys
import time


def make_profile(area, token):
    from observations_fixture import make_profile as cones_profile
    profile = cones_profile(area, token)
    namespace = "/dr_test_" + token
    profile["ros"]["domain_id"] = 226
    profile["ros"]["topics"] = profile["ros"]["topics"][:1]
    main = profile["units"][0]
    main["environment"]["ROS_DOMAIN_ID"] = "226"
    main["argv"][-3:] = [str(Path(__file__).resolve()), str(area), namespace, "cones"]
    peer = copy.deepcopy(main)
    peer.update(id="peer", label="Synthetic lidar")
    peer["argv"][-1] = "lidar"
    profile["units"].append(peer)
    profile["ros"]["topics"].append(dict(id="scan", label="Synthetic lidar returns", unit_id="peer", node=namespace+"/lidar", name=namespace+"/scan",
        type="sensor_msgs/msg/LaserScan", qos=dict(reliability="best_effort", durability="volatile", depth=1), max_rate_hz=10,
        stale_after_seconds=.5, max_serialized_bytes=65536, max_json_bytes=16384,
        projection=dict(kind="laser_scan", source_frame_id="laser", frame_id="base_link", translation_m=[.2,0,.12],
                        quaternion_xyzw=[0,0,1,0], max_points=360, timestamp_clock="system", timestamp_semantics="estimated_scan_start")))
    return profile


def main():
    import rclpy
    from rclpy.node import Node
    from rclpy.signals import SignalHandlerOptions
    from sensor_msgs.msg import LaserScan
    from robot_runtime_test_interfaces.msg import ConeDetections, ConeDetection
    area, namespace, role = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    assert area.parent.parent == Path("/tmp") and area.parent.name.startswith("robot-runtime-ros-")
    assert namespace.startswith("/dr_test_") and os.environ["ROS_DOMAIN_ID"] == "226"
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    node = Node("provider" if role == "cones" else "lidar", namespace=namespace, enable_rosout=False, start_parameter_services=False)
    name = namespace + ("/cones" if role == "cones" else "/scan")
    kind = ConeDetections if role == "cones" else LaserScan
    publisher = node.create_publisher(kind, name, 1)
    collision, frozen, stopped = None, None, False
    def stop(*_):
        nonlocal stopped
        stopped = True
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM): signal.signal(sig, stop)
    signal.alarm(300)
    with (area / "provider-events.jsonl").open("a") as out: out.write(json.dumps(dict(event="ready", role=role))+"\n")
    print("ready", flush=True)
    try:
        while not stopped:
            try: control = json.loads((area / (role+"-control.json")).read_text())
            except FileNotFoundError: control = {}
            if control.get("exit"): raise SystemExit(7)
            if control.get("collide") and collision is None: collision = node.create_publisher(kind, name, 1)
            elif not control.get("collide") and collision is not None:
                node.destroy_publisher(collision)
                collision = None
            message = kind()
            timestamp = time.time_ns() + int(control.get("stamp_offset", 0) * 1e9)
            if control.get("freeze_stamp"):
                frozen = frozen or timestamp
                timestamp = frozen
            else: frozen = None
            message.header.stamp.sec, message.header.stamp.nanosec = divmod(timestamp, 10**9)
            message.header.frame_id = control.get("frame", "base_link" if role == "cones" else "laser")
            if role == "cones":
                message.acquisition_to_publish_latency_s = .01
                item = ConeDetection()
                item.position.x, item.position.y, item.position.z = 1., 1., .2
                item.color, item.classification_confidence = 1, .8
                message.detections = [item] * control.get("count", 2)
            else:
                count = control.get("count", 720)
                message.angle_min = -math.pi
                message.angle_increment = 2 * math.pi / max(1, count)
                message.angle_max = message.angle_min + (count - 1) * message.angle_increment
                if control.get("bad_angles"): message.angle_max += .1
                message.range_min, message.range_max = .15, 12.
                message.scan_time, message.time_increment = .1, .1 / max(1, count)
                ranges = [float("inf") if control.get("empty") else 2.] * count
                if control.get("partial") and count >= 3: ranges[:3] = [float("nan"), .1, 13.]
                message.ranges = ranges
            if control.get("publish", True): publisher.publish(message)
            temporary = area / (role+"-status.tmp")
            temporary.write_text(json.dumps(dict(subscribers=publisher.get_subscription_count())))
            os.replace(temporary, area / (role+"-status.json"))
            rclpy.spin_once(node, timeout_sec=.05)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__": main()
