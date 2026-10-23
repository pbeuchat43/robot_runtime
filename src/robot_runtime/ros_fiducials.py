"""Bounded, structurally mapped fiducial poses in a declared fixed frame."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import math
import re

from .common import Fault, fields
from .ros_codec import scalar, selected_schema
from .ros_geometry import compose, transform, validate_mount

MAPPED = ("array_field", "dictionary_field", "id_field", "size_field", "pose_field", "valid_field", "error_field")


def validate_fiducials(spec):
    from .ros_profile import field_list, number
    value = spec["projection"]
    fields(value, ("kind", *MAPPED, "source_frame_id", "frame_id", "translation_m", "quaternion_xyzw", "max_items", "acquisition_clock"))
    for name in MAPPED: field_list([value[name]])
    validate_mount(value)
    number(value["max_items"], 1, 20, integer=True)
    if value["acquisition_clock"] != "system":
        raise Fault("invalid_ros_profile", "Fiducials require acquisition stamps in the host system clock")


def fiducial_schema(message, config, get_message):
    available = message.get_fields_and_field_types()
    match = re.fullmatch(r"sequence<([a-z][a-z0-9_]*/[A-Za-z][A-Za-z0-9_]*)(?:,\s*\d+)?>", available.get(config["array_field"], ""))
    if not match or available.get("header") != "std_msgs/Header" or available.get(config["dictionary_field"]) != "string":
        raise Fault("invalid_ros_profile", "Fiducials require a Header, dictionary string and pose-record sequence")
    package, name = match[1].split("/")
    item = get_message(package + "/msg/" + name)()
    if item.get_fields_and_field_types().get(config["pose_field"]) != "geometry_msgs/Pose":
        raise Fault("invalid_ros_profile", "Fiducials require geometry_msgs/Pose")
    kinds = selected_schema(item, [config[name] for name in ("id_field", "size_field", "valid_field", "error_field")])
    if (kinds[config["id_field"]] != "int32" or kinds[config["valid_field"]] not in ("boolean", "bool") or
        any(kinds[config[name]] not in ("float", "float32", "double", "float64") for name in ("size_field", "error_field"))):
        raise Fault("invalid_ros_profile", "Fiducials require an int32 ID, boolean validity, and floating size/error")
    return dict(projection=config)


def project_fiducials(message, config, timestamp):
    source = scalar(message.header.frame_id, "string<=128")
    if source != config["source_frame_id"]:
        raise Fault("frame_mismatch", "Fiducial frame differs from the declared camera mount")
    dictionary = scalar(getattr(message, config["dictionary_field"]), "string<=128")
    if not dictionary:
        raise Fault("invalid_sample", "Fiducial dictionary is missing")
    items = getattr(message, config["array_field"])
    detections = []
    for item in items[:config["max_items"]]:
        identifier = scalar(getattr(item, config["id_field"]), "int32")
        size = scalar(getattr(item, config["size_field"]), "double")
        valid = scalar(getattr(item, config["valid_field"]), "boolean")
        if identifier < 0 or size <= 0:
            raise Fault("invalid_sample", "Fiducial ID or physical size is invalid")
        position, orientation, error = None, None, None
        # A pose-invalid record carries provider placeholders and may carry
        # NaN error. Keep its identity/size without exposing a position at zero.
        if valid:
            pose = getattr(item, config["pose_field"])
            xyz = [scalar(getattr(pose.position, axis), "double") for axis in "xyz"]
            q = [scalar(getattr(pose.orientation, axis), "double") for axis in "xyzw"]
            if not .98 <= math.hypot(*q) <= 1.02:
                raise Fault("invalid_quaternion", "Fiducial orientation is not approximately a unit quaternion")
            error = scalar(getattr(item, config["error_field"]), "double")
            if error < 0:
                raise Fault("invalid_sample", "Reprojection error cannot be negative")
            position = dict(zip("xyz", transform(xyz, config)))
            orientation = compose(config["quaternion_xyzw"], q)
        detections.append(dict(id=identifier, marker_size_m=size, pose_valid=valid, position=position,
                               orientation_xyzw=orientation, reprojection_error_px=error))
    positioned = sum(item["pose_valid"] for item in detections)
    return dict(frame_id=config["frame_id"], source_frame_id=source, units="m", axes="x_forward_y_left_z_up",
                acquisition_timestamp_ns=timestamp, acquisition_clock="host_system", dictionary_name=dictionary,
                transform="declared_fixed_mount", translation_m=config["translation_m"], quaternion_xyzw=config["quaternion_xyzw"],
                orientation_convention="view_from_marker", detections=detections, original_count=len(items), displayed_count=len(detections),
                positioned_count=positioned, invalid_pose_count=len(detections)-positioned, truncated=len(items) > len(detections))
