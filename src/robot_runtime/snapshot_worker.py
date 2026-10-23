"""One requested image in a disposable ROS/OpenCV process, outside control work."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import sys
import os
import time
import uuid
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robot_runtime.common import Fault, dumps, loads
from robot_runtime.ipc import MAX_RESPONSE
from robot_runtime.snapshot_image import encode_image


def capture(spec, domain_id, deadline):
    import cv2
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import Image
    cv2.setNumThreads(1)
    rclpy.init(args=[], domain_id=domain_id)
    node = Node("_robot_snapshot_" + uuid.uuid4().hex[:12], enable_rosout=False,
                start_parameter_services=False, enable_logger_service=False)
    result = None
    rejected = False
    error = Fault("source_unavailable", "No fresh image arrived before the capture deadline")
    subscription = None

    def source():
        endpoints = node.get_publishers_info_by_topic(spec["name"])
        full = lambda name, namespace: namespace.rstrip("/") + "/" + name
        declared_nodes = sum(full(name, ns) == spec["node"] for name, ns in node.get_node_names_and_namespaces())
        if (len(endpoints) != 1 or endpoints[0].topic_type != spec["type"]
                or full(endpoints[0].node_name, endpoints[0].node_namespace) != spec["node"]
                or declared_nodes != 1):
            raise Fault("source_unavailable", "Snapshot requires exactly one declared graph publisher",
                        publishers=len(endpoints), declared_nodes=declared_nodes)
        return bytes(endpoints[0].endpoint_gid)

    def receive(raw, info):
        nonlocal result, error, rejected
        received, wall = time.monotonic(), time.time_ns()
        try:
            if len(raw) > spec["max_serialized_bytes"]:
                raise Fault("image_too_large", "Serialized image exceeds the declared input budget")
            gid = source()
            if info.get("publisher_gid") is not None and bytes(info["publisher_gid"]) != gid:
                raise Fault("source_unavailable", "Image publisher changed")
            result = encode_image(deserialize_message(raw, Image), spec, wall_ns=wall, received_monotonic=received)
            if source() != gid:
                result = None
                raise Fault("source_unavailable", "Image publisher changed during conversion")
        except Fault as exc:
            error = exc
            rejected = True
        except (ValueError, TypeError, RuntimeError):
            error = Fault("invalid_image", "Image deserialization or conversion failed")
            rejected = True

    try:
        # Do not join a stream until discovery has identified the declared source.
        while time.monotonic() < deadline and result is None and not rejected:
            if subscription is None:
                try:
                    source()
                    qos_profile_sensor_data.depth = 1
                    subscription = node.create_subscription(Image, spec["name"], receive, qos_profile_sensor_data, raw=True)
                except Fault as exc:
                    error = exc
            rclpy.spin_once(node, timeout_sec=0.1)
        if result is None:
            raise error
        return result
    finally:
        node.destroy_node()
        rclpy.shutdown()


def main():
    try:
        raw = sys.stdin.buffer.readline(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE or not raw.endswith(b"\n"):
            raise Fault("invalid_request", "Invalid snapshot worker request")
        request = loads(raw)
        # Linux parent-death notification prevents an in-flight private image
        # helper surviving an abruptly lost manager and overlapping its recovery.
        import ctypes
        import signal
        # Ordinary cancellation keeps ROS's graceful SIGTERM handler so DDS
        # promptly withdraws the subscription. Abrupt parent death instead kills
        # only this private helper, including any native conversion in flight.
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != request["manager_pid"]:
            raise Fault("snapshot_unavailable", "Snapshot manager is no longer available")
        result = capture(request["spec"], request["domain_id"], request["deadline"])
        response = dict(ok=True, result=result)
    except Fault as exc:
        response = dict(ok=False, error=exc.as_dict())
    except Exception:
        response = dict(ok=False, error=dict(code="snapshot_unavailable", message="Snapshot helper failed; inspect the manager journal"))
    print(dumps(response), flush=True)


if __name__ == "__main__":
    main()
