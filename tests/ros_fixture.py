"""Finite, isolated ROS provider and its test profile; no hardware access."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import argparse
import json
import os
import signal
import time
from pathlib import Path


def make_profile(area, token, *, include_limits=True):
    launch = area / "launch-ros.sh"
    launch.write_text('set -e\nsource /opt/ros/jazzy/setup.bash\nexec /usr/bin/python3 "$@"\n')
    namespace = "/dr_test_" + token
    environment = dict(PATH="/usr/bin:/bin", HOME=str(Path.home()), ROS_DOMAIN_ID="221", ROS_AUTOMATIC_DISCOVERY_RANGE="LOCALHOST",
                       ROS_STATIC_PEERS="", ROS_LOG_DIR=str(area / "provider-logs"), PYTHONNOUSERSITE="1", RCUTILS_LOGGING_USE_STDOUT="0")
    config = dict(schema_version=1, robot_id="ros-fixture", namespace="r"+token, units=[
        dict(id="idle", label="Synthetic ROS provider", argv=["/bin/bash", "--noprofile", "--norc", str(launch),
             str(Path(__file__).resolve()), str(area), namespace], working_directory=str(area), environment=environment,
             stop=dict(signal="SIGINT", timeout_seconds=3), public_logs=True)])
    topic = dict(id="counter", label="64-bit counter", unit_id="idle", node=namespace+"/provider", name=namespace+"/counter",
                 type="std_msgs/msg/Int64", fields=["data"], qos=dict(reliability="best_effort", durability="volatile", depth=1),
                 max_rate_hz=5, stale_after_seconds=0.6, max_serialized_bytes=1024, max_json_bytes=1024)
    topics = [topic]
    if include_limits:
        topics.append(dict(topic, id="text", label="Bounded text", name=namespace+"/text", type="std_msgs/msg/String",
                           max_serialized_bytes=128, max_json_bytes=64))
    config["ros"] = dict(schema_version=1, python="/usr/bin/python3", setup_scripts=["/opt/ros/jazzy/setup.bash"],
                         domain_id=221, discovery="LOCALHOST", topics=topics,
                         services=[dict(id="enable", label="Request provider enable", unit_id="idle", node=namespace+"/provider",
                                        name=namespace+"/enable", type="std_srvs/srv/SetBool", request_fields=["data"],
                                        response_fields=["success", "message"], success_field="success", timeout_seconds=1)])
    (area/"profile.json").write_text(json.dumps(config))
    return config


def main():
    import rclpy
    from rclpy.node import Node
    from rclpy.signals import SignalHandlerOptions
    from std_msgs.msg import Int64, String
    from std_srvs.srv import SetBool
    parser = argparse.ArgumentParser()
    parser.add_argument("area", type=Path)
    parser.add_argument("namespace")
    parser.add_argument("--duplicate", action="store_true")
    args = parser.parse_args()
    area = args.area.resolve()
    assert area.parent.parent == Path("/tmp") and area.parent.name.startswith("robot-runtime-ros-")
    assert args.namespace.startswith("/dr_test_")
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    node = Node("provider", namespace=args.namespace, enable_rosout=False, start_parameter_services=False)
    counter_pub = node.create_publisher(Int64, args.namespace+"/counter", 1)
    text_pub = node.create_publisher(String, args.namespace+"/text", 1)
    stopped = False
    count, calls = 0, 0
    status_path = area / ("duplicate-status.json" if args.duplicate else "provider-status.json")
    events = (area / ("duplicate-events.jsonl" if args.duplicate else "provider-events.jsonl")).open("a")

    def control():
        try:
            return json.loads((area/"control.json").read_text())
        except FileNotFoundError:
            return {}

    def record(event, **values):
        value = dict(event=event, monotonic=time.monotonic(), pid=os.getpid(), **values)
        events.write(json.dumps(value)+"\n")
        events.flush()
        print(json.dumps(value), flush=True)

    def status():
        value = dict(calls=calls, published=count, counter_subscribers=counter_pub.get_subscription_count(),
                     text_subscribers=text_pub.get_subscription_count())
        temporary = status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value))
        os.replace(temporary, status_path)

    def service(request, response):
        nonlocal calls
        calls += 1
        record("service_received", calls=calls, data=request.data)
        status()
        time.sleep(min(float(control().get("delay", 0)), 3))
        response.success = request.data
        response.message = "Synthetic provider accepted" if request.data else "Synthetic provider refused"
        record("service_response", calls=calls, success=response.success)
        return response

    node.create_service(SetBool, args.namespace+"/enable", service)
    def stop(*_args):
        nonlocal stopped
        stopped = True
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
        signal.signal(number, stop)
    signal.alarm(300)
    record("ready")
    next_publish, next_status = 0, 0
    try:
        while not stopped:
            now = time.monotonic()
            if now >= next_publish:
                value = control()
                if value.get("publish", True):
                    count += 1
                    counter_pub.publish(Int64(data=9007199254740993+count))
                    text_pub.publish(String(data=value.get("text", "hello")))
                next_publish = now+0.05
            if now >= next_status:
                status()
                next_status = now+0.1
            rclpy.spin_once(node, timeout_sec=0.01)
    finally:
        record("exit")
        status()
        node.destroy_node()
        rclpy.shutdown()
        events.close()


if __name__ == "__main__":
    main()
