"""Small, explicit ROS scalar wire format; this module has no ROS dependency."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import math
import re

from .common import Fault, dumps, fields


def scalar_type(kind):
    integer = re.fullmatch(r"(u?)int(8|16|32|64)", kind)
    if integer:
        return "integer", int(integer[2]), bool(integer[1])
    string = re.fullmatch(r"string(?:<=(\d+))?", kind)
    if string:
        return "string", min(int(string[1]), 4096) if string[1] else 4096, False
    if kind in ("boolean", "bool"):
        return "boolean", 0, False
    if kind in ("float", "float32", "double", "float64"):
        return "float", 32 if kind in ("float", "float32") else 64, False
    raise Fault("unsupported_type", "Only primitive scalar fields are supported", field_type=kind)


def scalar(value, kind, *, to_ros=False):
    category, size, unsigned = scalar_type(kind)
    valid = False
    if category == "boolean":
        valid = type(value) is bool
    elif category == "string":
        valid = isinstance(value, str) and len(value) <= size and "\0" not in value
    elif category == "float":
        maximum = 3.4028234663852886e38 if size == 32 else 1.7976931348623157e308
        valid = type(value) in (int, float) and abs(value) <= maximum and math.isfinite(value)
        if valid:
            value = float(value)
    else:
        if size == 64 and to_ros:
            if not isinstance(value, str) or not re.fullmatch(r"0|-?[1-9][0-9]{0,19}", value):
                raise Fault("invalid_arguments", "64-bit integers require canonical decimal strings")
            value = int(value)
        minimum, maximum = (0, 2**size - 1) if unsigned else (-2**(size - 1), 2**(size - 1) - 1)
        valid = type(value) is int and minimum <= value <= maximum
        if valid and size == 64 and not to_ros:
            return str(value)
    if not valid:
        raise Fault("invalid_arguments", "Field does not match its declared ROS scalar type", field_type=kind)
    return value


def selected_schema(message, names, *, complete=False):
    available = message.get_fields_and_field_types()
    if not set(names) <= set(available) or complete and set(names) != set(available):
        raise Fault("invalid_ros_profile", "Selected fields do not match the ROS interface")
    result = {name: available[name] for name in names}
    for kind in result.values():
        scalar_type(kind)
    return result


def arguments(value, schema):
    fields(value, schema)
    if len(dumps(value).encode()) > 8192:
        raise Fault("invalid_arguments", "ROS arguments exceed 8 KiB")
    return {name: scalar(value[name], kind, to_ros=True) for name, kind in schema.items()}


def projection(message, schema, maximum=8192):
    value = {name: scalar(getattr(message, name), kind) for name, kind in schema.items()}
    if len(dumps(value).encode()) > maximum:
        raise Fault("sample_too_large", "Selected ROS data exceeds the JSON byte limit")
    return value
