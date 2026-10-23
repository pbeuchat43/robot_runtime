"""Bounded LaserScan projection through a declared, fixed mounting transform."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import math

from .common import Fault, fields
from .ros_codec import scalar
from .ros_geometry import transform, validate_mount


def validate_scan(spec):
    from .ros_profile import number
    value = spec["projection"]
    fields(value, ("kind", "source_frame_id", "frame_id", "translation_m", "quaternion_xyzw", "max_points", "timestamp_clock", "timestamp_semantics"))
    if spec["type"] != "sensor_msgs/msg/LaserScan":
        raise Fault("invalid_ros_profile", "Laser scan projections require sensor_msgs/LaserScan")
    validate_mount(value)
    number(value["max_points"], 1, 360, integer=True)
    if value["timestamp_clock"] != "system" or value["timestamp_semantics"] != "estimated_scan_start":
        raise Fault("invalid_ros_profile", "Declare the host-clock scan-start estimate")


def project_scan(message, config, timestamp):
    frame = scalar(message.header.frame_id, "string<=128")
    if frame != config["source_frame_id"]:
        raise Fault("frame_mismatch", "Scan frame differs from the declared mounting frame")
    metadata = {name: scalar(getattr(message, name), "float") for name in
                ("angle_min", "angle_max", "angle_increment", "range_min", "range_max", "scan_time", "time_increment")}
    count = len(message.ranges)
    increment = metadata["angle_increment"]
    if count > 8192 or metadata["range_min"] < 0 or metadata["range_max"] <= metadata["range_min"] or min(metadata["scan_time"], metadata["time_increment"]) < 0:
        raise Fault("invalid_sample", "Scan bounds or timing are invalid")
    if count > 1 and (increment == 0 or abs(increment) * (count - 1) > 2 * math.pi + .01):
        raise Fault("invalid_sample", "Scan angular extent is invalid")
    if count and abs(metadata["angle_min"] + (count - 1) * increment - metadata["angle_max"]) > max(1e-5, abs(increment) * .001):
        raise Fault("invalid_sample", "Scan angles do not match the range array")
    # Missing/out-of-range returns are normal LaserScan data, not zero ranges.
    valid = [(index, float(distance)) for index, distance in enumerate(message.ranges)
             if math.isfinite(distance) and metadata["range_min"] <= distance <= metadata["range_max"]]
    limit = config["max_points"]
    indices = ([round(i * (len(valid) - 1) / (limit - 1)) for i in range(limit)] if limit > 1 else [0]) if len(valid) > limit else range(len(valid))
    points = []
    for selected in indices:
        index, distance = valid[selected]
        angle = metadata["angle_min"] + index * increment
        x, y = distance * math.cos(angle), distance * math.sin(angle)
        # Rotate the planar ray with q_target_from_source, then translate.
        xyz = transform((x, y, 0), config)
        points.append([*(round(value, 6) for value in xyz), index])
    return dict(frame_id=config["frame_id"], source_frame_id=frame, units="m", axes="x_forward_y_left_z_up",
                acquisition_timestamp_ns=timestamp, acquisition_clock="host_system", timestamp_semantics=config["timestamp_semantics"],
                transform="declared_fixed_mount", translation_m=config["translation_m"], quaternion_xyzw=config["quaternion_xyzw"],
                points=points, point_columns=["x", "y", "z", "ray_index"], original_count=count, valid_count=len(valid),
                displayed_count=len(points), downsampled=len(valid) > len(points), sampling="evenly_spaced_valid_ray_indices",
                **metadata)
