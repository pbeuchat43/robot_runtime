"""Bounded image configuration and JPEG conversion; native libraries load on demand."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import base64

from .common import Fault, fields, text


def validate_snapshots(specs, identifiers, unit_ids):
    from .profile import identifier
    from .ros_profile import number, ros_name
    if not isinstance(specs, list) or len(specs) > 4:
        raise Fault("invalid_ros_profile", "Declare at most four snapshot sources")
    for spec in specs:
        fields(spec, ("id", "label", "unit_id", "node", "name", "type", "frame_id", "acquisition_clock",
                      "timeout_seconds", "stale_after_seconds", "max_serialized_bytes", "max_width", "max_height",
                      "max_jpeg_bytes", "jpeg_quality"), ("required_mode",))
        identifier(spec["id"])
        identifier(spec["unit_id"])
        if spec["id"] in identifiers or spec["unit_id"] not in unit_ids:
            raise Fault("invalid_ros_profile", "Duplicate capability ID or unknown runtime unit")
        identifiers.add(spec["id"])
        text(spec["label"], 120)
        text(spec["frame_id"], 128)
        ros_name(spec["node"])
        ros_name(spec["name"])
        if "required_mode" in spec:
            identifier(spec["required_mode"])
        if spec["type"] != "sensor_msgs/msg/Image" or spec["acquisition_clock"] != "system":
            raise Fault("invalid_ros_profile", "Snapshots require sensor_msgs/Image with host system acquisition time")
        number(spec["timeout_seconds"], 1, 8)
        number(spec["stale_after_seconds"], 0.2, 5)
        number(spec["max_serialized_bytes"], 1024, 8388608, integer=True)
        number(spec["max_width"], 64, 960, integer=True)
        number(spec["max_height"], 64, 720, integer=True)
        number(spec["max_jpeg_bytes"], 1024, 131072, integer=True)
        number(spec["jpeg_quality"], 40, 90, integer=True)


def encode_image(message, spec, *, wall_ns, received_monotonic):
    """Validate before allocating a pixel array; preserve acquisition identity."""
    stamp = message.header.stamp
    timestamp = int(stamp.sec) * 1000000000 + int(stamp.nanosec)
    age = (wall_ns - timestamp) / 1000000000
    if (stamp.sec <= 0 or not 0 <= stamp.nanosec < 1000000000 or age < -0.1
            or age > spec["stale_after_seconds"]):
        raise Fault("stale_snapshot", "Image acquisition timestamp is missing, future or stale")
    if message.header.frame_id != spec["frame_id"]:
        raise Fault("invalid_image", "Image frame does not match the declared frame")
    channels = {"bgr8": 3, "rgb8": 3, "mono8": 1}.get(message.encoding)
    width, height, step = message.width, message.height, message.step
    if (not channels or not 1 <= width <= 4096 or not 1 <= height <= 4096 or step < width * channels
            or step * height != len(message.data) or len(message.data) > spec["max_serialized_bytes"]):
        raise Fault("invalid_image", "Unsupported encoding or invalid image dimensions, stride or data length")
    import cv2
    import numpy as np
    pixels = np.frombuffer(message.data, dtype=np.uint8).reshape(height, step)[:, :width * channels]
    pixels = pixels.reshape(height, width, channels) if channels == 3 else pixels.reshape(height, width)
    scale = min(1, spec["max_width"] / width, spec["max_height"] / height)
    output_width, output_height = max(1, int(width * scale)), max(1, int(height * scale))
    if scale < 1:
        pixels = cv2.resize(pixels, (output_width, output_height), interpolation=cv2.INTER_AREA)
    if message.encoding == "rgb8":
        pixels = cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR)
    ok, encoded = cv2.imencode(".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY, spec["jpeg_quality"]])
    if not ok or encoded.nbytes > spec["max_jpeg_bytes"]:
        raise Fault("image_too_large", "Encoded image exceeds the declared JPEG budget")
    return dict(mime_type="image/jpeg", image_base64=base64.b64encode(encoded).decode("ascii"),
                jpeg_bytes=int(encoded.nbytes), width=output_width, height=output_height,
                source_width=width, source_height=height, source_encoding=message.encoding,
                frame_id=message.header.frame_id, acquisition_timestamp_ns=str(timestamp),
                acquisition_clock="host_system", acquisition_age_at_receipt=max(0, age),
                received_monotonic=received_monotonic)
