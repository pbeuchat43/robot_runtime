"""Bounded spatial and diagnostic projections; no native ROS dependency."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import math
import re

from .common import Fault, dumps, fields, text
from .ros_codec import scalar, selected_schema


def validate_projection(spec):
    from .ros_profile import field_list, number
    value = spec["projection"]
    if not isinstance(value, dict):
        raise Fault("invalid_ros_profile", "Declare a bounded topic projection")
    if value.get("kind") == "detections":
        names = ("array_field", "position_field", "color_field", "confidence_field", "latency_field")
        fields(value, ("kind", *names, "frame_id", "max_items", "forward_m", "half_width_m", "acquisition_clock"))
        for name in names:
            field_list([value[name]])
        text(value["frame_id"], 128)
        number(value["max_items"], 1, 20, integer=True)
        number(value["forward_m"], 1, 50)
        number(value["half_width_m"], 1, 25)
        if value["acquisition_clock"] != "system":
            raise Fault("invalid_ros_profile", "Detections currently require acquisition stamps in the host system clock")
    elif value.get("kind") == "laser_scan":
        from .ros_scan import validate_scan
        validate_scan(spec)
    elif value.get("kind") == "fiducials":
        from .ros_fiducials import validate_fiducials
        validate_fiducials(spec)
    elif value.get("kind") == "diagnostic":
        fields(value, ("kind", "status_name", "keys", "progress_key"))
        if spec["type"] != "diagnostic_msgs/msg/DiagnosticArray":
            raise Fault("invalid_ros_profile", "Diagnostic projections require DiagnosticArray")
        text(value["status_name"], 256)
        keys = value["keys"]
        if not isinstance(keys, list) or not 1 <= len(keys) <= 16:
            raise Fault("invalid_ros_profile", "Expose one to sixteen diagnostic keys")
        for key in keys:
            text(key, 128)
        if len(set(keys)) != len(keys) or (value["progress_key"] is not None and value["progress_key"] not in keys):
            raise Fault("invalid_ros_profile", "Declare unique keys and a selected cumulative counter, or null when unavailable")
    elif value.get("kind") == "imu":
        fields(value, ("kind", "frame_id", "timestamp_clock", "timestamp_semantics", "orientation_reference", "acceleration_semantics"))
        if spec["type"] != "sensor_msgs/msg/Imu":
            raise Fault("invalid_ros_profile", "IMU projections require sensor_msgs/Imu")
        text(value["frame_id"], 128)
        text(value["orientation_reference"], 120)
        if value["timestamp_clock"] != "system" or value["timestamp_semantics"] not in ("acquisition", "estimated_mcu_callback_receipt"):
            raise Fault("invalid_ros_profile", "Declare IMU stamps in the host system clock and their actual meaning")
        if value["acceleration_semantics"] not in ("specific_force", "linear_acceleration"):
            raise Fault("invalid_ros_profile", "Declare whether acceleration includes gravity")
    else:
        raise Fault("invalid_ros_profile", "Unknown topic projection")


def observation_schema(message, spec, get_message):
    """Check native interface shape once, before subscribing."""
    projection = spec["projection"]
    if projection["kind"] == "fiducials":
        from .ros_fiducials import fiducial_schema
        return fiducial_schema(message, projection, get_message)
    if projection["kind"] in ("diagnostic", "imu", "laser_scan"):
        return dict(projection=projection)
    available = message.get_fields_and_field_types()
    array = available.get(projection["array_field"], "")
    match = re.fullmatch(r"sequence<([a-z][a-z0-9_]*/[A-Za-z][A-Za-z0-9_]*)(?:,\s*\d+)?>", array)
    if not match or available.get("header") != "std_msgs/Header":
        raise Fault("invalid_ros_profile", "Detections require a Header and a sequence of point records")
    package, name = match[1].split("/")
    item = get_message(package + "/msg/" + name)()
    item_fields = item.get_fields_and_field_types()
    if item_fields.get(projection["position_field"]) != "geometry_msgs/Point":
        raise Fault("invalid_ros_profile", "Detection positions must be geometry_msgs/Point")
    kinds = selected_schema(item, [projection["color_field"], projection["confidence_field"]])
    if kinds[projection["color_field"]] != "uint8" or kinds[projection["confidence_field"]] not in ("float", "float32", "double", "float64"):
        raise Fault("invalid_ros_profile", "Detections require uint8 color and floating confidence")
    latency = selected_schema(message, [projection["latency_field"]])
    if latency[projection["latency_field"]] not in ("float", "float32", "double", "float64"):
        raise Fault("invalid_ros_profile", "Detection latency must be floating point seconds")
    return dict(projection=projection)


def stamp_ns(header):
    seconds = scalar(header.stamp.sec, "int32")
    nanoseconds = scalar(header.stamp.nanosec, "uint32")
    if seconds < 0 or not 0 <= nanoseconds < 10**9 or seconds == nanoseconds == 0:
        raise Fault("invalid_sample", "Header timestamp is invalid")
    return str(seconds * 10**9 + nanoseconds)


def imu_field(message, name, axes):
    """Respect the standard missing-field sentinel; never reuse another sample."""
    covariance = getattr(message, name + "_covariance")
    # Native fixed ROS arrays are NumPy arrays in Jazzy. Convert their scalar
    # representation here; do not relax the shared browser-command codec.
    if hasattr(covariance, "tolist"):
        covariance = covariance.tolist()
    covariance = [scalar(value, "double") for value in covariance]
    if len(covariance) != 9:
        raise Fault("invalid_sample", "IMU covariance must contain nine values")
    if covariance[0] == -1:
        return None
    if any(covariance[index] < 0 for index in (0, 4, 8)):
        raise Fault("invalid_sample", "Present IMU covariance has a negative diagonal")
    value = {axis: scalar(getattr(getattr(message, name), axis), "double") for axis in axes}
    if name == "orientation":
        norm = math.hypot(*value.values())
        if not 0.98 <= norm <= 1.02:
            raise Fault("invalid_quaternion", "Present orientation is not approximately a unit quaternion")
        value["norm"] = norm
    return dict(value=value, covariance=covariance, covariance_known=any(covariance))


def project_observation(message, spec):
    config = spec["projection"]
    if config["kind"] == "detections":
        frame = scalar(message.header.frame_id, "string<=128")
        if frame != config["frame_id"]:
            raise Fault("frame_mismatch", "Detection frame differs from the declared view frame")
        items = getattr(message, config["array_field"])
        points = []
        # Preserve provider order. Count and truncation are always explicit.
        for item in items[:config["max_items"]]:
            position = getattr(item, config["position_field"])
            xyz = {axis: scalar(getattr(position, axis), "double") for axis in ("x", "y", "z")}
            confidence = scalar(getattr(item, config["confidence_field"]), "double")
            if not 0 <= confidence <= 1:
                raise Fault("invalid_sample", "Detection confidence is outside [0, 1]")
            points.append(dict(**xyz, color=scalar(getattr(item, config["color_field"]), "uint8"), confidence=confidence))
        latency = scalar(getattr(message, config["latency_field"]), "double")
        if latency < 0:
            raise Fault("invalid_sample", "Acquisition-to-publish latency is negative")
        value = dict(frame_id=frame, units="m", axes="x_forward_y_left_z_up", acquisition_timestamp_ns=stamp_ns(message.header),
                     acquisition_clock="host_system", acquisition_to_publish_latency_s=latency,
                     detections=points, original_count=len(items), displayed_count=len(points), truncated=len(items) > len(points))
    elif config["kind"] == "laser_scan":
        from .ros_scan import project_scan
        value = project_scan(message, config, stamp_ns(message.header))
    elif config["kind"] == "fiducials":
        from .ros_fiducials import project_fiducials
        value = project_fiducials(message, config, stamp_ns(message.header))
    elif config["kind"] == "imu":
        frame = scalar(message.header.frame_id, "string<=128")
        if frame != config["frame_id"]:
            raise Fault("frame_mismatch", "IMU frame differs from the declared sensor frame")
        value = dict(frame_id=frame, sample_timestamp_ns=stamp_ns(message.header), sample_clock="host_system",
                     timestamp_semantics=config["timestamp_semantics"], orientation_reference=config["orientation_reference"],
                     quaternion_convention="reference_from_sensor", acceleration_semantics=config["acceleration_semantics"],
                     orientation=imu_field(message, "orientation", "xyzw"),
                     angular_velocity=imu_field(message, "angular_velocity", "xyz"),
                     linear_acceleration=imu_field(message, "linear_acceleration", "xyz"))
        if all(value[name] is None for name in ("orientation", "angular_velocity", "linear_acceleration")):
            raise Fault("measurement_unavailable", "The IMU message contains no available measurements")
    else:
        matches = [item for item in message.status if item.name == config["status_name"]]
        if len(matches) != 1:
            raise Fault("diagnostic_unavailable", "Expected exactly one declared diagnostic status")
        item = matches[0]
        level = int.from_bytes(item.level, "little") if isinstance(item.level, bytes) else item.level
        if type(level) is not int or not 0 <= level <= 3:
            raise Fault("invalid_sample", "Unknown diagnostic level")
        values = {}
        for pair in item.values:
            if pair.key in config["keys"]:
                if pair.key in values:
                    raise Fault("invalid_sample", "Duplicate diagnostic key")
                values[pair.key] = scalar(pair.value, "string<=512")
        value = dict(name=config["status_name"], level=level, message=scalar(item.message, "string<=512"), values=values)
        if config["progress_key"] is not None:
            counter = values.get(config["progress_key"], "")
            if not re.fullmatch(r"0|[1-9][0-9]{0,19}", counter):
                raise Fault("invalid_sample", "Missing or invalid diagnostic progress counter")
            value["progress_counter"] = counter
    if len(dumps(value).encode()) > spec["max_json_bytes"]:
        raise Fault("sample_too_large", "Projected observation exceeds the JSON byte limit")
    return value
