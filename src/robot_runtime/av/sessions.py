"""Bounded ephemeral sessions, bound to browser authority and a service run."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import secrets
import threading
import time

from ..common import Fault, dumps

AUTH_TTL = 3.0
MEDIA_TTL = 20.0


class Sessions:
    def __init__(self, maximum, *, clock=time.monotonic):
        self.maximum, self.clock = maximum, clock
        self.lock = threading.RLock()
        self.entries = {}

    def alive(self, entry):
        now = self.clock()
        return now < entry["authorized_until"] and (entry["kind"] != "media" or now < entry["until"])

    def acquire(self, kind, context, request_id, value):
        with self.lock:
            fingerprint = dumps(value)
            for token, entry in self.entries.items():
                if (entry["gateway"], entry["owner"], entry["request_id"]) == (context["gateway"], context["owner"], request_id):
                    if entry["fingerprint"] != fingerprint or not self.alive(entry):
                        raise Fault("request_conflict", "Session request expired or was used with different arguments")
                    return token, entry, False
            # Expired native peers must be cleaned up before their capacity is reused.
            if len(self.entries) >= self.maximum:
                raise Fault("busy", "The configured session limit has been reached")
            token = secrets.token_urlsafe(32)
            entry = dict(kind=kind, gateway=context["gateway"], owner=context["owner"],
                         request_id=request_id, fingerprint=fingerprint, source_run=context.get("source_run"),
                         created=self.clock(), authorized_until=self.clock() + AUTH_TTL,
                         until=self.clock() + MEDIA_TTL, response=None, websocket=None,
                         profile=value.get("profile"))
            self.entries[token] = entry
            return token, entry, True

    def require(self, token, context=None):
        with self.lock:
            entry = self.entries.get(token) if isinstance(token, str) else None
            if not entry or not self.alive(entry):
                raise Fault("unauthorized", "The session expired; connect explicitly again")
            if context is not None and (entry["owner"], entry["gateway"], entry["source_run"]) != (
                    context["owner"], context["gateway"], context.get("source_run")):
                raise Fault("forbidden", "This session belongs to another browser or source run")
            return entry

    def allowed(self, token):
        try:
            self.require(token)
            return True
        except Fault:
            return False

    def renew(self, token, context):
        with self.lock:
            self.require(token, context)["until"] = self.clock() + MEDIA_TTL

    def heartbeat(self, gateway, owners, source_run):
        with self.lock:
            for entry in self.entries.values():
                if entry["gateway"] == gateway:
                    valid = entry["owner"] in owners and entry["source_run"] == source_run and self.alive(entry)
                    entry["authorized_until"] = self.clock() + AUTH_TTL if valid else 0

    def invalidate(self, gateway):
        with self.lock:
            for entry in self.entries.values():
                if entry["gateway"] == gateway:
                    entry["authorized_until"] = 0

    def remove(self, token):
        with self.lock:
            return self.entries.pop(token, None)

    def expired(self):
        with self.lock:
            return [token for token, entry in self.entries.items() if not self.alive(entry)]

    def summary(self):
        with self.lock:
            return dict(subscriptions=len(self.entries), maximum=self.maximum,
                        authorization_ttl_s=AUTH_TTL, actuators_enabled=False)
