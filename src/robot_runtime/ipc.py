"""Bounded private Unix-socket client; commands are sent once, never retried."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import socket
from pathlib import Path

from .common import Fault, dumps, loads

MAX_REQUEST = 16384
# Allow the 256 KiB aggregate topic payload plus catalog and observation metadata.
MAX_RESPONSE = 524288


def decode_response(raw):
    if not raw.endswith(b"\n") or len(raw) > MAX_RESPONSE:
        raise Fault("transport_unavailable", "Incomplete or oversized manager response")
    response = loads(raw)
    if not isinstance(response, dict) or type(response.get("ok")) is not bool:
        raise Fault("transport_unavailable", "Invalid manager response")
    if not response["ok"]:
        error = response["error"]
        raise Fault(error["code"], error["message"], **{k: v for k, v in error.items() if k not in ("code", "message")})
    return response["result"]


def request(state, action, **values):
    message = (dumps(dict(version=1, action=action, **values)) + "\n").encode()
    if len(message) > MAX_REQUEST:
        raise Fault("invalid_request", "Request exceeds 16 KiB")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(15)
            connection.connect(str(Path(state) / "runtime.sock"))
            connection.sendall(message)
            with connection.makefile("rb") as reader:
                return decode_response(reader.readline(MAX_RESPONSE + 1))
    except (OSError, TimeoutError) as exc:
        raise Fault("transport_unknown", "Manager response unavailable; query the request ID before any new command") from exc


def watch(state, action="watch", **values):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(15)
        connection.connect(str(Path(state) / "runtime.sock"))
        raw = (dumps(dict(version=1, action=action, **values)) + "\n").encode()
        if len(raw) > MAX_REQUEST:
            raise Fault("invalid_request", "Watch request exceeds 16 KiB")
        connection.sendall(raw)
        with connection.makefile("rb") as reader:
            while True:
                value = decode_response(reader.readline(MAX_RESPONSE + 1))
                if value.get("type") == "snapshot":
                    yield value["status"]
