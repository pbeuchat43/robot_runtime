"""Offline shared credentials and independent, bounded browser sessions."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import getpass
import hashlib
import hmac
import os
import secrets
import time
import uuid
from pathlib import Path

from .common import Fault, dumps, fields, loads

COOKIE = "__Host-robot_runtime"


def password_hash(password, salt):
    # OWASP's 32 MiB scrypt option. Authentication runs off the web event loop.
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=32768, r=8, p=3, maxmem=64 * 1024 * 1024).hex()


def write_credential(path, password):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise Fault("invalid_credential", "Use a passphrase of 12–256 characters")
    salt = secrets.token_bytes(16)
    value = dict(version=1, scheme="scrypt-32768-8-3", salt=salt.hex(), digest=password_hash(password, salt))
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(dumps(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def create_credential(path):
    password = getpass.getpass("New robot passphrase: ")
    if password != getpass.getpass("Repeat robot passphrase: "):
        raise Fault("invalid_credential", "Passphrases do not match")
    write_credential(path, password)


class Auth:
    def __init__(self, path, *, clock=time.monotonic):
        self.path = Path(path)
        self.clock = clock
        self.signature = None
        self.sessions = {}
        self.tokens = 5.0
        self.last_refill = clock()
        self.reload()

    def reload(self):
        stat = self.path.stat()
        if self.path.is_symlink() or not self.path.is_file() or stat.st_uid != os.getuid() or stat.st_mode & 0o077 or stat.st_size > 2048:
            raise Fault("unsafe_credential", "Credential file must be a private regular file owned by this account")
        signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        if signature != self.signature:
            value = loads(self.path.read_bytes())
            fields(value, ("version", "scheme", "salt", "digest"))
            if value["version"] != 1 or value["scheme"] != "scrypt-32768-8-3":
                raise Fault("unsafe_credential", "Unsupported credential format")
            try:
                if len(bytes.fromhex(value["salt"])) != 16 or len(bytes.fromhex(value["digest"])) != 64:
                    raise ValueError()
            except (ValueError, TypeError) as exc:
                raise Fault("unsafe_credential", "Invalid credential value") from exc
            self.value = value
            self.signature = signature
            self.sessions.clear()

    def allow_login(self):
        now = self.clock()
        self.tokens = min(5.0, self.tokens + (now - self.last_refill) / 10)
        self.last_refill = now
        if self.tokens < 1:
            raise Fault("rate_limited", "Wait before trying another passphrase")
        self.tokens -= 1

    def verify(self, password):
        if not isinstance(password, str) or not 1 <= len(password) <= 256:
            return False
        value = self.value
        return hmac.compare_digest(password_hash(password, bytes.fromhex(value["salt"])), value["digest"])

    def issue(self):
        self.expire()
        if len(self.sessions) >= 32:
            raise Fault("session_limit", "Browser session limit reached")
        token = secrets.token_urlsafe(32)
        now = self.clock()
        session = dict(id=str(uuid.uuid4()), csrf=secrets.token_urlsafe(24), created=now, touched=now)
        self.sessions[hashlib.sha256(token.encode()).hexdigest()] = session
        return token, session

    def expire(self):
        now = self.clock()
        for key, session in list(self.sessions.items()):
            if now - session["created"] >= 8 * 3600 or now - session["touched"] >= 1800:
                del self.sessions[key]

    def session(self, token, *, touch=False):
        self.expire()
        if not isinstance(token, str) or len(token) > 100:
            return None
        session = self.sessions.get(hashlib.sha256(token.encode()).hexdigest())
        if session and touch:
            session["touched"] = self.clock()
        return session

    def revoke(self, token):
        if token:
            self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)
