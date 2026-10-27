"""Optional ROS capability schema, validated without loading native ROS code."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import math
import os
import re
from pathlib import Path

from .common import Fault, fields, text

MAX_TOPICS = 48


def number(value, low, high, *, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) or not low <= value <= high or not math.isfinite(value):
        raise Fault("invalid_ros_profile", "ROS limit is outside its supported range")


def ros_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:/[A-Za-z_][A-Za-z0-9_]*)+", value) or len(value) > 256:
        raise Fault("invalid_ros_profile", "ROS names must be explicit absolute names without substitutions")


def field_list(value, *, empty=False):
    if not isinstance(value, list) or not (0 if empty else 1) <= len(value) <= 16:
        raise Fault("invalid_ros_profile", "Declare unique exposed ROS fields")
    for name in value:
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name) or len(name) > 64:
            raise Fault("invalid_ros_profile", "Only direct primitive field names are supported")
    if len(set(value)) != len(value):
        raise Fault("invalid_ros_profile", "Declare unique exposed ROS fields")


def validate(source, unit_ids, *, check_paths=True):
    # Import here to avoid an import cycle with the base profile parser.
    from .profile import absolute, identifier
    fields(source, ("schema_version", "python", "setup_scripts", "domain_id", "discovery", "topics", "services"), ("parameters", "snapshots", "actions", "commands"))
    if type(source["schema_version"]) is not int or source["schema_version"] != 1:
        raise Fault("invalid_ros_profile", "Unsupported ROS schema")
    absolute(source["python"])
    number(source["domain_id"], 0, 232, integer=True)
    if source["discovery"] != "LOCALHOST":
        raise Fault("invalid_ros_profile", "This slice supports localhost ROS discovery only")
    scripts = source["setup_scripts"]
    if not isinstance(scripts, list) or not 1 <= len(scripts) <= 4:
        raise Fault("invalid_ros_profile", "Declare one to four trusted ROS setup scripts")
    for script in scripts:
        absolute(script)
    if check_paths:
        if not Path(source["python"]).is_file() or not os.access(source["python"], os.X_OK):
            raise Fault("invalid_ros_profile", "ROS interpreter is unavailable")
        if any(not Path(script).is_file() or not os.access(script, os.R_OK) for script in scripts):
            raise Fault("invalid_ros_profile", "A ROS setup script is unavailable")
    identifiers = set()
    for category, interface in (("topics", "msg"), ("services", "srv")):
        capabilities = source[category]
        if not isinstance(capabilities, list) or len(capabilities) > (MAX_TOPICS if category == "topics" else 8):
            raise Fault("invalid_ros_profile", "At most 48 topics and eight services are supported")
        for spec in capabilities:
            shared = ("id", "label", "unit_id", "node", "name", "type")
            if category == "topics":
                fields(spec, (*shared, "qos", "max_rate_hz", "stale_after_seconds", "max_serialized_bytes", "max_json_bytes"), ("fields", "projection", "active_modes"))
                if "active_modes" in spec:
                    modes = spec["active_modes"]
                    if not isinstance(modes, list) or not 1 <= len(modes) <= 8:
                        raise Fault("invalid_ros_profile", "Topic active modes require one to eight declared launches")
                    for mode in modes:
                        identifier(mode)
                    if len(set(modes)) != len(modes):
                        raise Fault("invalid_ros_profile", "Topic active modes must be unique")
                if ("fields" in spec) == ("projection" in spec):
                    raise Fault("invalid_ros_profile", "Choose scalar fields or one bounded projection")
                if "projection" in spec:
                    from .ros_observations import validate_projection
                    validate_projection(spec)
                else:
                    field_list(spec["fields"])
                fields(spec["qos"], ("reliability", "durability", "depth"))
                if spec["qos"]["reliability"] not in ("reliable", "best_effort") or spec["qos"]["durability"] != "volatile" or type(spec["qos"]["depth"]) is not int or spec["qos"]["depth"] != 1:
                    raise Fault("invalid_ros_profile", "Topics require explicit reliability and volatile depth-one QoS")
                # A 20 Hz IMU view can honor a provider's 100 ms expiry. Other
                # existing topic profiles retain their ten-Hz conversion cap.
                number(spec["max_rate_hz"], 0.2, 20 if spec.get("projection", {}).get("kind") == "imu" else 10)
                number(spec["stale_after_seconds"], 0.1, 60)
                number(spec["max_serialized_bytes"], 64, 65536, integer=True)
                number(spec["max_json_bytes"], 64, 16384, integer=True)
            else:
                fields(spec, (*shared, "request_fields", "response_fields", "timeout_seconds"), ("success_field",))
                field_list(spec["request_fields"], empty=True)
                field_list(spec["response_fields"], empty=True)
                number(spec["timeout_seconds"], 0.1, 10)
                if "success_field" in spec and spec["success_field"] not in spec["response_fields"]:
                    raise Fault("invalid_ros_profile", "Success field must be an exposed boolean response field")
            identifier(spec["id"])
            identifier(spec["unit_id"])
            if spec["id"] in identifiers or spec["unit_id"] not in unit_ids:
                raise Fault("invalid_ros_profile", "Duplicate capability ID or unknown runtime unit")
            identifiers.add(spec["id"])
            text(spec["label"], 120)
            ros_name(spec["node"])
            ros_name(spec["name"])
            if not isinstance(spec["type"], str) or not re.fullmatch(r"[a-z][a-z0-9_]*/" + interface + r"/[A-Za-z][A-Za-z0-9_]*", spec["type"]):
                raise Fault("invalid_ros_profile", "Declare an exact ROS interface type")
    if sum(spec["max_json_bytes"] for spec in source["topics"]) > 262144:
        raise Fault("invalid_ros_profile", "Combined topic payload limits must fit within 256 KiB")
    commands = source.get("commands", [])
    if not isinstance(commands, list) or len(commands) > 8:
        raise Fault("invalid_ros_profile", "At most eight topic command capabilities are supported")
    for spec in commands:
        fields(spec, ("id", "label", "unit_id", "node", "name", "type", "fields", "choices", "timeout_seconds"))
        identifier(spec["id"])
        identifier(spec["unit_id"])
        if spec["id"] in identifiers or spec["unit_id"] not in unit_ids:
            raise Fault("invalid_ros_profile", "Duplicate capability ID or unknown runtime unit")
        identifiers.add(spec["id"])
        text(spec["label"], 120)
        ros_name(spec["node"])
        ros_name(spec["name"])
        if not isinstance(spec["type"], str) or not re.fullmatch(r"[a-z][a-z0-9_]*/msg/[A-Za-z][A-Za-z0-9_]*", spec["type"]):
            raise Fault("invalid_ros_profile", "Declare an exact ROS message type")
        field_list(spec["fields"])
        number(spec["timeout_seconds"], 1, 5)
        if not isinstance(spec["choices"], list) or not 1 <= len(spec["choices"]) <= 8:
            raise Fault("invalid_ros_profile", "Declare one to eight fixed command choices")
        choices = set()
        for choice in spec["choices"]:
            fields(choice, ("id", "label", "arguments"))
            identifier(choice["id"])
            text(choice["label"], 120)
            fields(choice["arguments"], spec["fields"])
            if choice["id"] in choices:
                raise Fault("invalid_ros_profile", "Command choice IDs must be unique")
            choices.add(choice["id"])
    actions = source.get("actions", [])
    if not isinstance(actions, list) or len(actions) > 4:
        raise Fault("invalid_ros_profile", "At most four action capabilities are supported")
    for spec in actions:
        fields(spec, ("id", "label", "unit_id", "node", "name", "type", "goal_fields", "result_fields",
                      "feedback_fields", "response_timeout_seconds", "max_feedback_rate_hz", "stale_after_seconds"))
        identifier(spec["id"])
        identifier(spec["unit_id"])
        if spec["id"] in identifiers or spec["unit_id"] not in unit_ids:
            raise Fault("invalid_ros_profile", "Duplicate capability ID or unknown runtime unit")
        identifiers.add(spec["id"])
        text(spec["label"], 120)
        ros_name(spec["node"])
        ros_name(spec["name"])
        if not isinstance(spec["type"], str) or not re.fullmatch(r"[a-z][a-z0-9_]*/action/[A-Za-z][A-Za-z0-9_]*", spec["type"]):
            raise Fault("invalid_ros_profile", "Declare an exact ROS action type")
        for key in ("goal_fields", "result_fields", "feedback_fields"):
            field_list(spec[key], empty=True)
        number(spec["response_timeout_seconds"], 0.5, 10)
        number(spec["max_feedback_rate_hz"], 0.2, 5)
        number(spec["stale_after_seconds"], 0.5, 60)
    parameters = source.get("parameters", [])
    if not isinstance(parameters, list) or len(parameters) > 12:
        raise Fault("invalid_ros_profile", "At most twelve parameter node capabilities are supported")
    for spec in parameters:
        fields(spec, ("id", "label", "unit_id", "node", "parameters", "timeout_seconds", "stale_after_seconds"))
        identifier(spec["id"])
        identifier(spec["unit_id"])
        if spec["id"] in identifiers or spec["unit_id"] not in unit_ids:
            raise Fault("invalid_ros_profile", "Duplicate capability ID or unknown runtime unit")
        identifiers.add(spec["id"])
        text(spec["label"], 120)
        ros_name(spec["node"])
        number(spec["timeout_seconds"], 0.5, 10)
        number(spec["stale_after_seconds"], 1, 60)
        if not isinstance(spec["parameters"], list) or not 1 <= len(spec["parameters"]) <= 16:
            raise Fault("invalid_ros_profile", "Expose one to sixteen named parameters per node capability")
        names = set()
        for item in spec["parameters"]:
            fields(item, ("name", "write"))
            name = text(item["name"], 128)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", name) or name in names or type(item["write"]) is not bool:
                raise Fault("invalid_ros_profile", "Use unique explicit parameter names and boolean write permissions")
            names.add(name)
    from .snapshot_image import validate_snapshots
    validate_snapshots(source.get("snapshots", []), identifiers, unit_ids)
    if not identifiers:
        raise Fault("invalid_ros_profile", "Declare at least one ROS capability")
    return source
