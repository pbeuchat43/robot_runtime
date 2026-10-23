"""Bounded, lossless parameter values and descriptors; no native ROS imports."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
from .common import Fault, dumps, fields, text
from .ros_codec import scalar


TYPES = ("not_set", "bool", "integer", "double", "string", "byte_array", "bool_array",
         "integer_array", "double_array", "string_array")
SCALARS = dict(bool="boolean", integer="int64", double="double", string="string", byte="uint8")
MAX_JSON = 16384
MAX_ARRAY = 128


def bounded(value):
    if len(dumps(value).encode()) > MAX_JSON:
        raise Fault("parameter_limit", "Parameter data exceeds the 16 KiB result limit")
    return value


def typed(value, *, to_ros=False):
    fields(value, ("type", "value"))
    kind, data = value["type"], value["value"]
    if not isinstance(kind, str) or kind not in TYPES:
        raise Fault("invalid_arguments", "Unknown ROS parameter type")
    if kind == "not_set":
        if to_ros or data is not None:
            raise Fault("invalid_arguments", "Unsetting or declaring parameters is not supported")
    elif kind.endswith("_array"):
        if not isinstance(data, list) or len(data) > MAX_ARRAY:
            raise Fault("parameter_limit", "Parameter arrays are limited to 128 elements")
        data = [scalar(item, SCALARS[kind[:-6]], to_ros=to_ros) for item in data]
    else:
        data = scalar(data, SCALARS[kind], to_ros=to_ros)
    return bounded(dict(type=kind, value=data))


def from_message(message):
    if type(message.type) is not int or not 0 <= message.type < len(TYPES):
        raise Fault("invalid_response", "Provider returned an unknown parameter type")
    kind = TYPES[message.type]
    if kind == "not_set":
        return dict(type=kind, value=None)
    value = getattr(message, kind + "_value")
    if kind.endswith("_array"):
        if len(value) > MAX_ARRAY:
            raise Fault("parameter_limit", "Provider parameter array exceeds 128 elements")
        # ROS byte[] uses one-byte bytes objects in some generated bindings.
        value = [item[0] if isinstance(item, bytes) and len(item) == 1 else item for item in value]
    return typed(dict(type=kind, value=value))


def descriptor(message, expected_name):
    if message.name != expected_name or not 0 <= message.type < len(TYPES):
        raise Fault("invalid_response", "Parameter descriptor does not match the requested name/type")
    if len(message.integer_range) > 1 or len(message.floating_point_range) > 1:
        raise Fault("invalid_response", "Invalid parameter descriptor ranges")
    return dict(declared_type=TYPES[message.type],
        description=text(message.description, 1024, empty=True),
        additional_constraints=text(message.additional_constraints, 1024, empty=True),
        read_only=bool(message.read_only), dynamic_typing=bool(message.dynamic_typing),
        integer_range=[dict(from_value=scalar(r.from_value, "int64"), to_value=scalar(r.to_value, "int64"),
                            step=scalar(r.step, "uint64")) for r in message.integer_range],
        floating_point_range=[dict(from_value=scalar(r.from_value, "double"), to_value=scalar(r.to_value, "double"),
                                   step=scalar(r.step, "double")) for r in message.floating_point_range])


def inspection(names, values, descriptors, permissions):
    if len(values) != len(names) or len(descriptors) != len(names):
        raise Fault("invalid_response", "Provider did not return every declared parameter")
    result = {}
    for name, value, description in zip(names, values, descriptors):
        item = from_message(value)
        meta = descriptor(description, name)
        if not meta["dynamic_typing"] and item["type"] not in ("not_set", meta["declared_type"]):
            raise Fault("invalid_response", "Parameter type changed during inspection; refresh")
        result[name] = dict(item, descriptor=meta,
                            writable=bool(permissions[name] and not meta["read_only"] and item["type"] != "not_set"))
    return bounded(result)


def changes(value, baseline, permissions):
    if not isinstance(value, dict) or not value or len(value) > 16:
        raise Fault("invalid_arguments", "Choose one to sixteen declared parameters to change")
    result = {}
    for name, item in value.items():
        if name not in permissions or name not in baseline:
            raise Fault("not_found", "Parameter is not declared for this capability")
        current = baseline[name]
        if not permissions[name] or not current["writable"] or current["descriptor"]["read_only"]:
            raise Fault("read_only", "Parameter is read-only", parameter=name)
        normalized = typed(item, to_ros=True)
        if normalized["type"] != current["type"]:
            raise Fault("invalid_arguments", "Refresh and preserve the current parameter type", parameter=name)
        result[name] = normalized
    # Check the wire form, whose decimal-string integer representation is bounded.
    bounded(value)
    return result
