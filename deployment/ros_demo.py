"""Finite, synthetic ROS demo for the persistent development installation."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import json
import os
import signal
import time

import rclpy
from rclpy.node import Node
from rclpy.signals import SignalHandlerOptions
from rcl_interfaces.msg import FloatingPointRange, IntegerRange, ParameterDescriptor
from std_msgs.msg import Int64
from std_srvs.srv import SetBool


def main():
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    node = Node("provider", namespace="/robot_runtime_dev", enable_rosout=False)
    node.declare_parameter("rate_hz", 20.0, ParameterDescriptor(description="Synthetic counter publish rate in Hz",
        floating_point_range=[FloatingPointRange(from_value=1.0, to_value=40.0, step=0.0)]))
    node.declare_parameter("increment", 1, ParameterDescriptor(description="Counter increment per publication",
        integer_range=[IntegerRange(from_value=1, to_value=100, step=1)]))
    node.declare_parameter("identity", "synthetic-demo", ParameterDescriptor(description="Provider identity", read_only=True))
    publisher = node.create_publisher(Int64, "/robot_runtime_dev/counter", 1)
    stopped, enabled, count = False, True, 0

    def record(event, **values):
        print(json.dumps(dict(event=event, pid=os.getpid(), **values)), flush=True)

    def enable(request, response):
        nonlocal enabled
        enabled = request.data
        response.success = True
        response.message = "Synthetic counter publishing " + ("enabled" if enabled else "paused")
        record("publishing", enabled=enabled)
        return response

    def stop(*_args):
        nonlocal stopped
        stopped = True

    node.create_service(SetBool, "/robot_runtime_dev/enable", enable)
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
        signal.signal(number, stop)
    signal.alarm(600)
    record("ready", lifetime_seconds=600)
    next_publish = 0
    try:
        while not stopped:
            now = time.monotonic()
            if now >= next_publish:
                if enabled:
                    count += node.get_parameter("increment").value
                    publisher.publish(Int64(data=9007199254740993 + count))
                next_publish = now + 1.0 / node.get_parameter("rate_hz").value
            rclpy.spin_once(node, timeout_sec=0.01)
    finally:
        record("exit", published=count)
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
