"""Small shared value and error helpers; no supervisor or web dependencies."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import json


class Fault(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self):
        return dict(code=self.code, message=self.message, **self.details)


def dumps(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True)


def loads(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = item
        return result

    def constant(_value):
        raise ValueError("non-finite JSON number")

    try:
        return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Fault("invalid_json", "Expected finite JSON with unique fields") from exc


def fields(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise Fault("invalid_request", "Missing or unsupported fields")


def text(value, maximum=256, empty=False):
    if not isinstance(value, str) or len(value) > maximum or "\0" in value or (not empty and not value):
        raise Fault("invalid_request", "Invalid text value")
    return value
