"""Finite synthetic image provider in isolated ROS domain 224; no camera access."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import json
import os
from pathlib import Path
import signal
import sys
import time


def make_profile(area, token):
    launch = area / "launch-images.sh"
    launch.write_text('set -e\nsource /opt/ros/jazzy/setup.bash\nexec /usr/bin/python3 "$@"\n')
    namespace = "/dr_test_" + token
    argv = ["/bin/bash", "--noprofile", "--norc", str(launch), str(Path(__file__).resolve()), str(area), namespace]
    spec = dict(id="image", label="Synthetic annotated image", unit_id="idle", node=namespace + "/provider", name=namespace + "/image",
                type="sensor_msgs/msg/Image", frame_id="camera", acquisition_clock="system", required_mode="images",
                timeout_seconds=8, stale_after_seconds=2, max_serialized_bytes=1048576,
                max_width=640, max_height=480, max_jpeg_bytes=131072, jpeg_quality=80)
    return dict(schema_version=1, robot_id="snapshot-fixture", namespace="i" + token, units=[
        dict(id="idle", label="Synthetic image provider", argv=argv,
             launch_modes=[dict(id="images", label="Start with images", argv=argv + ["images"])],
             working_directory=str(area), environment=dict(PATH="/usr/bin:/bin", HOME=str(Path.home()), ROS_DOMAIN_ID="224",
             ROS_AUTOMATIC_DISCOVERY_RANGE="LOCALHOST", ROS_STATIC_PEERS="", ROS_LOG_DIR=str(area / "ros-logs"), PYTHONNOUSERSITE="1"),
             stop=dict(signal="SIGINT", timeout_seconds=3), public_logs=True)],
        ros=dict(schema_version=1, python="/usr/bin/python3", setup_scripts=["/opt/ros/jazzy/setup.bash"], domain_id=224,
                 discovery="LOCALHOST", topics=[dict(id="counter", label="Control-path counter", unit_id="idle", node=namespace + "/provider",
                 name=namespace + "/counter", type="std_msgs/msg/UInt64", fields=["data"],
                 qos=dict(reliability="best_effort", durability="volatile", depth=1), max_rate_hz=10, stale_after_seconds=1,
                 max_serialized_bytes=128, max_json_bytes=128)], services=[], snapshots=[spec]))


def main():
    import cv2
    import numpy as np
    import rclpy
    from rclpy.node import Node
    from rclpy.signals import SignalHandlerOptions
    from sensor_msgs.msg import Image
    from std_msgs.msg import UInt64
    area, namespace = Path(sys.argv[1]), sys.argv[2]
    enabled = len(sys.argv) == 4 and sys.argv[3] == "images"
    assert area.parent.parent == Path("/tmp") and area.parent.name.startswith("robot-runtime-ros-")
    assert namespace.startswith("/dr_test_") and os.environ["ROS_DOMAIN_ID"] == "224"
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    node = Node("provider", namespace=namespace, enable_rosout=False, start_parameter_services=False)
    publisher = node.create_publisher(Image, namespace + "/image", 1) if enabled else None
    counter_publisher = node.create_publisher(UInt64, namespace + "/counter", 1)
    stopped, counter, maximum = False, 0, 0
    def stop(*_):
        nonlocal stopped
        stopped = True
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
        signal.signal(sig, stop)
    signal.alarm(300)
    with (area / "provider-events.jsonl").open("a") as events:
        events.write(json.dumps(dict(event="ready", images=enabled)) + "\n")
    print("ready", flush=True)
    try:
        while not stopped:
            try:
                control = json.loads((area / "control.json").read_text())
            except FileNotFoundError:
                control = {}
            counter += 1
            counter_publisher.publish(UInt64(data=counter))
            count = publisher.get_subscription_count() if publisher else 0
            maximum = max(maximum, count)
            if publisher and control.get("publish", True):
                width, height = (1280, 720) if control.get("oversize") else (640, 480)
                pixels = np.zeros((height, width, 3), dtype=np.uint8)
                pixels[:] = (50, 60, 70)
                cv2.rectangle(pixels, (150, 120), (450, 400), (0, 220, 255), 4)
                cv2.putText(pixels, "Synthetic cone 0.95", (150, 105), cv2.FONT_HERSHEY_SIMPLEX, .8, (0, 220, 255), 2)
                message = Image(width=width, height=height, encoding=control.get("encoding", "bgr8"), step=width * 3, data=pixels.tobytes())
                stamp = time.time_ns() + int(control.get("stamp_offset", 0) * 1e9)
                message.header.stamp.sec, message.header.stamp.nanosec = divmod(stamp, 10**9)
                message.header.frame_id = control.get("frame", "camera")
                publisher.publish(message)
            temporary = area / "provider-status.tmp"
            temporary.write_text(json.dumps(dict(image_subscribers=count, maximum_subscribers=maximum,
                                                  images_enabled=enabled, counter=counter)))
            os.replace(temporary, area / "provider-status.json")
            rclpy.spin_once(node, timeout_sec=0.1)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
