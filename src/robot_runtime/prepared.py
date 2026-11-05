"""Bounded, immutable launch inputs; their contents belong to the component."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import hashlib
import os
from pathlib import Path
import selectors
import shutil
import stat
import subprocess
import time
import uuid

from .common import Fault, dumps, fields, loads
from .profile import absolute, identifier

MAX_INPUT = 262144
MAX_TOTAL = 1048576


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_file(path, maximum=MAX_INPUT):
    """Open one ordinary file and read once, rejecting links and oversized input."""
    absolute(str(path))
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise Fault("invalid_prepared", "Prepared inputs must be regular files")
            data = stream.read(maximum + 1)
    except OSError as exc:
        raise Fault("invalid_prepared", "Prepared input is unavailable", path=str(path)) from exc
    if len(data) > maximum:
        raise Fault("invalid_prepared", "Prepared input exceeds its byte limit")
    return data


def validate(candidate, unit_id, revision, mode):
    fields(candidate, ("schema_version", "unit_id", "config_revision", "mode", "inputs"))
    if type(candidate["schema_version"]) is not int or candidate["schema_version"] != 1:
        raise Fault("invalid_prepared", "Unsupported prepared input schema")
    if (candidate["unit_id"], candidate["config_revision"], candidate["mode"]) != (unit_id, revision, mode):
        raise Fault("stale_prepared", "Prepared inputs target a different unit, profile or mode")
    if not isinstance(candidate["inputs"], list) or not 1 <= len(candidate["inputs"]) <= 8:
        raise Fault("invalid_prepared", "Provide one to eight named input files")
    files = {}
    for item in candidate["inputs"]:
        fields(item, ("name", "path", "sha256"))
        name = identifier(item["name"])
        if name in files:
            raise Fault("invalid_prepared", "Prepared input names must be unique")
        data = read_file(item["path"])
        if digest(data) != item["sha256"]:
            raise Fault("invalid_prepared", "Prepared input hash mismatch", name=name)
        files[name] = data
    if sum(map(len, files.values())) > MAX_TOTAL:
        raise Fault("invalid_prepared", "Prepared inputs exceed one MiB in total")
    return files


def freeze(candidate, directory, unit_id, revision, mode):
    """Copy accepted inputs into runtime state before any lifecycle mutation."""
    files = validate(candidate, unit_id, revision, mode)
    identity = digest(dumps(candidate).encode())
    parent = Path(directory) / "prepared"
    parent.mkdir(mode=0o700, exist_ok=True)
    destination = parent / identity
    manifest = dict(candidate, inputs=[dict(name=name, path=str(destination / name), sha256=digest(data))
                                      for name, data in files.items()])
    raw = dumps(manifest).encode()
    reference = dict(path=str(destination / "manifest.json"), sha256=digest(raw), identity=identity)
    if destination.exists():
        load(reference)
        return reference
    temporary = parent / (".prepare-" + uuid.uuid4().hex)
    temporary.mkdir(mode=0o700)
    try:
        for name, data in [*files.items(), ("manifest.json", raw)]:
            path = temporary / name
            with path.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            path.chmod(0o400)
        temporary.rename(destination)
        destination.chmod(0o500)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return reference


def load(reference):
    """Validate an accepted reference and return its manifest and named bytes."""
    raw = read_file(reference["path"])
    if digest(raw) != reference["sha256"]:
        raise Fault("invalid_prepared", "Prepared manifest hash mismatch")
    candidate = loads(raw)
    return candidate, validate(candidate, candidate["unit_id"], candidate["config_revision"], candidate["mode"])


def run_inputs():
    """Component-side accessor for the supervisor's prepared run context."""
    path = os.environ.get("ROBOT_RUNTIME_PREPARED")
    if not path:
        raise Fault("invalid_prepared", "This launch requires prepared runtime inputs")
    return load(dict(path=path, sha256=os.environ.get("ROBOT_RUNTIME_PREPARED_SHA256")))


def execute(target):
    """Run a profile-owned preparation command on an IPC worker, never GLib."""
    env = dict(target["environment"], ROBOT_RUNTIME_PREPARE_UNIT=target["unit_id"],
               ROBOT_RUNTIME_PREPARE_REVISION=target["config_revision"],
               ROBOT_RUNTIME_PREPARE_MODE=target["mode"] or "")
    process = subprocess.Popen(target["preparation"]["argv"], cwd=target["working_directory"], env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    output, error = bytearray(), bytearray()
    try:
        deadline = time.monotonic() + target["preparation"]["timeout_seconds"]
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, output)
            selector.register(process.stderr, selectors.EVENT_READ, error)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise Fault("preparation_failed", "Launch preparation exceeded its deadline; current run is unchanged")
                for key, _event in selector.select(0.05):
                    part = os.read(key.fileobj.fileno(), 8192)
                    if part:
                        key.data.extend(part)
                        if len(output) > 16384 or len(error) > 16384:
                            raise Fault("preparation_failed", "Launch preparation exceeded its output limit")
                    else:
                        selector.unregister(key.fileobj)
        if process.wait(timeout=max(.1, deadline - time.monotonic())):
            raise Fault("preparation_failed", "Launch preparation failed; inspect the component's configuration",
                        detail=error.decode(errors="replace")[-2048:])
        return loads(output)
    finally:
        # Only this finite preparation group, never a managed workload.
        import signal
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        process.stdout.close()
        process.stderr.close()
