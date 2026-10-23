"""Finite native Jazzy parameter node, with explicit fault controls for tests."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import json
import os
from pathlib import Path
import signal
import sys
import time


NAMES = ("enabled", "count", "rate", "title", "bytes", "flags", "counts", "rates", "titles", "identity", "private")


def make_profile(area, token):
    from ros_fixture import make_profile as base
    profile = base(area, token, include_limits=False)
    profile["units"][0]["argv"][4] = str(Path(__file__).resolve())
    profile["units"][0]["environment"]["ROS_DOMAIN_ID"] = "222"
    profile["ros"].update(domain_id=222, topics=[], services=[], parameters=[dict(
        id="settings", label="Synthetic parameter settings", unit_id="idle", node="/dr_test_" + token + "/provider",
        parameters=[dict(name=name, write=name != "private") for name in NAMES], timeout_seconds=3, stale_after_seconds=15)])
    (area / "profile.json").write_text(json.dumps(profile))
    return profile


def main():
    import rclpy
    from rclpy.node import Node
    from rclpy.parameter import Parameter
    from rclpy.signals import SignalHandlerOptions
    from rcl_interfaces.msg import FloatingPointRange, IntegerRange, ParameterDescriptor, SetParametersResult
    from rcl_interfaces.srv import DescribeParameters, GetParameters, SetParametersAtomically
    area, namespace = Path(sys.argv[1]).resolve(), sys.argv[2]
    assert area.parent.parent == Path("/tmp") and area.parent.name.startswith("robot-runtime-ros-")
    assert namespace.startswith("/dr_test_")
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    node = Node("provider", namespace=namespace, enable_rosout=False, start_parameter_services=False)
    values = [True, 9007199254740993, 5.0, "demo", [b"\x00", b"\xff"], [True, False],
              [-(2**63), 2**63-1], [0.5, 1.0], ["a", "b"], "fixture", 42]
    for name, value in zip(NAMES, values):
        meta = ParameterDescriptor(description="Synthetic " + name, read_only=name == "identity")
        if name == "rate":
            meta.floating_point_range = [FloatingPointRange(from_value=1.0, to_value=20.0, step=0.0)]
            meta.additional_constraints = "Rate 13 is rejected by the provider callback"
        if name == "private":
            meta.integer_range = [IntegerRange(from_value=0, to_value=100, step=1)]
        node.declare_parameter(name, value, meta)
    counts = dict(get=0, describe=0, set=0)
    stopped, readback = False, False
    last_external = None

    def control():
        try:
            return json.loads((area / "control.json").read_text())
        except FileNotFoundError:
            return {}

    def status():
        path = area / "provider-status.tmp"
        path.write_text(json.dumps(dict(counts=counts, title=node.get_parameter("title").value,
            count=str(node.get_parameter("count").value), rate=node.get_parameter("rate").value)))
        os.replace(path, area / "provider-status.json")

    def validate(parameters):
        bad = any(p.name == "rate" and p.value == 13.0 for p in parameters)
        return SetParametersResult(successful=not bad, reason="Synthetic rejection of rate 13" if bad else "")
    node.add_on_set_parameters_callback(validate)

    def get(request, response):
        nonlocal readback
        counts["get"] += 1; status()
        delay = control().get("readback_delay", 0) if readback else control().get("read_delay", 0)
        time.sleep(min(float(delay), 4))
        readback = False
        response.values = [p.get_parameter_value() for p in node.get_parameters(request.names)]
        return response

    def describe(request, response):
        counts["describe"] += 1; status()
        response.descriptors = node.describe_parameters(request.names)
        return response

    def atomic(request, response):
        nonlocal readback
        counts["set"] += 1; status()
        time.sleep(min(float(control().get("set_delay", 0)), 4))
        response.result = node.set_parameters_atomically([Parameter.from_parameter_msg(p) for p in request.parameters])
        if response.result.successful:
            if control().get("mismatch"):
                node.set_parameters_atomically([Parameter("title", value="external writer after acceptance")])
            readback = True
        status()
        return response

    node.create_service(GetParameters, node.get_fully_qualified_name() + "/get_parameters", get)
    node.create_service(DescribeParameters, node.get_fully_qualified_name() + "/describe_parameters", describe)
    node.create_service(SetParametersAtomically, node.get_fully_qualified_name() + "/set_parameters_atomically", atomic)
    def stop(*_args):
        nonlocal stopped
        stopped = True
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
        signal.signal(number, stop)
    signal.alarm(300)
    with (area / "provider-events.jsonl").open("a") as events:
        events.write(json.dumps(dict(event="ready", pid=os.getpid())) + "\n")
    print(json.dumps(dict(event="ready", pid=os.getpid())), flush=True)
    try:
        while not stopped:
            value = control()
            if "external_title" in value and value["external_title"] != last_external:
                last_external = value["external_title"]
                node.set_parameters_atomically([Parameter("title", value=last_external)])
            status()
            rclpy.spin_once(node, timeout_sec=0.02)
    finally:
        node.destroy_node(); rclpy.shutdown()


if __name__ == "__main__":
    main()
