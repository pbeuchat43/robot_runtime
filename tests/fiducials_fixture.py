"""Finite native fiducials alongside the isolated domain-226 cone/lidar fixtures."""
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
    from lidar_fixture import make_profile as spatial_profile
    profile = spatial_profile(area, token)
    namespace = "/dr_test_" + token
    marker = copy.deepcopy(profile["units"][0])
    marker.update(id="markers", label="Synthetic fiducials")
    marker["argv"][-4:] = [str(Path(__file__).resolve()), str(area), namespace]
    profile["units"].append(marker)
    profile["ros"]["topics"].append(dict(id="fiducials", label="Synthetic marker poses", unit_id="markers", node=namespace+"/fiducials",
        name=namespace+"/fiducials", type="robot_runtime_test_interfaces/msg/FiducialDetections",
        qos=dict(reliability="reliable", durability="volatile", depth=1), max_rate_hz=10,
        stale_after_seconds=.75, max_serialized_bytes=65536, max_json_bytes=16384,
        projection=dict(kind="fiducials", array_field="detections", dictionary_field="dictionary_name", id_field="id",
            size_field="marker_size_m", pose_field="pose", valid_field="pose_valid", error_field="reprojection_error_px",
            source_frame_id="camera_optical", frame_id="base_link", translation_m=[.2,0,.3], quaternion_xyzw=[-.5,.5,-.5,.5],
            max_items=20, acquisition_clock="system")))
    return profile


def main():
    import rclpy
    from rclpy.node import Node
    from rclpy.signals import SignalHandlerOptions
    from robot_runtime_test_interfaces.msg import FiducialDetection, FiducialDetections
    area, namespace = Path(sys.argv[1]), sys.argv[2]
    assert area.parent.parent == Path("/tmp") and area.parent.name.startswith("robot-runtime-ros-")
    assert namespace.startswith("/dr_test_") and os.environ["ROS_DOMAIN_ID"] == "226"
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    node = Node("fiducials", namespace=namespace, enable_rosout=False, start_parameter_services=False)
    publisher = node.create_publisher(FiducialDetections, namespace+"/fiducials", 1)
    stopped, collision, frozen = False, None, None
    def stop(*_):
        nonlocal stopped
        stopped = True
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM): signal.signal(sig, stop)
    signal.alarm(300)
    print("ready", flush=True)
    try:
        while not stopped:
            try: control = json.loads((area / "markers-control.json").read_text())
            except FileNotFoundError: control = {}
            if control.get("exit"): raise SystemExit(7)
            if control.get("collide") and collision is None:
                collision = node.create_publisher(FiducialDetections, namespace+"/fiducials", 1)
            elif not control.get("collide") and collision is not None:
                node.destroy_publisher(collision)
                collision = None
            message = FiducialDetections()
            stamp = time.time_ns() + int(control.get("stamp_offset", 0)*1e9)
            if control.get("freeze_stamp"):
                frozen = frozen or stamp
                stamp = frozen
            else: frozen = None
            message.header.stamp.sec, message.header.stamp.nanosec = divmod(stamp, 10**9)
            message.header.frame_id = control.get("frame", "camera_optical")
            message.dictionary_name = control.get("dictionary", "DICT_4X4_50")
            for index in range(control.get("count", 2)):
                item = FiducialDetection()
                item.id = 7  # Repeated IDs are deliberately separate observations.
                item.marker_size_m = .081 if index == 0 else .162
                item.pose_valid = not control.get("invalid_pose", False)
                item.pose.position.x, item.pose.position.y, item.pose.position.z = -.2, -.1-index*.4, 1.5
                item.pose.orientation.y = math.sin(math.pi/8) if index else 0.
                item.pose.orientation.w = math.cos(math.pi/8) if index else 1.
                item.reprojection_error_px = float("nan") if not item.pose_valid else .4
                if control.get("bad_quaternion"): item.pose.orientation.w = 2.
                message.detections.append(item)
            if control.get("publish", True): publisher.publish(message)
            temporary = area / "markers-status.tmp"
            temporary.write_text(json.dumps(dict(subscribers=publisher.get_subscription_count())))
            os.replace(temporary, area / "markers-status.json")
            rclpy.spin_once(node, timeout_sec=.05)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__": main()
