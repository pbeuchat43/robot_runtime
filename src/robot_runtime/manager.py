"""One GLib policy owner and a bounded local IPC adapter."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import argparse
import os
import queue
import select
import signal
import socket
import socketserver
import struct
import sys
import threading
from pathlib import Path

from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

from . import journal, profile
from .common import Fault, dumps, fields, loads, text
from .core import Core
from .ipc import MAX_REQUEST, MAX_RESPONSE
from .store import Store
from .supervisor import Systemd, owns
from .resource_monitor import ResourceMonitor
from .ros_process import RosProcess
from .ros_runtime import RosRuntime
from .snapshot_runtime import SnapshotProcess


class Hub:
    def __init__(self, core):
        self.core = core
        self.queues = set()

    def add(self):
        channel = queue.Queue(maxsize=1)
        self.queues.add(channel)
        channel.put(self.core.status())
        return channel

    def publish(self):
        value = self.core.status()
        for channel in tuple(self.queues):
            try:
                channel.get_nowait()
            except queue.Empty:
                pass
            channel.put_nowait(value)


class Dispatcher:
    def __init__(self, core):
        self.core = core
        self.hub = Hub(core)
        self.closed = threading.Event()
        core.on_change = self.hub.publish

    def call(self, function):
        done = threading.Event()
        result = []
        def execute():
            try:
                result.append((True, function()))
            except Exception as exc:
                result.append((False, exc))
            finally:
                done.set()
            return GLib.SOURCE_REMOVE
        GLib.idle_add(execute)
        if not done.wait(12):
            raise Fault("transport_unknown", "Manager reply deadline exceeded; query command history")
        ok, value = result[0]
        if not ok:
            raise value
        return value


class Handler(socketserver.StreamRequestHandler):
    def setup(self):
        self.request.settimeout(4)
        super().setup()

    def send(self, value):
        raw = (dumps(value) + "\n").encode()
        if len(raw) > MAX_RESPONSE:
            raise Fault("response_limit", "Response exceeded the IPC byte limit")
        self.wfile.write(raw)
        self.wfile.flush()

    def handle(self):
        dispatcher = self.server.dispatcher
        try:
            _pid, uid, _gid = struct.unpack("3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                raise Fault("forbidden", "Local runtime access requires the runtime account")
            raw = self.rfile.readline(MAX_REQUEST + 1)
            if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
                raise Fault("invalid_request", "Expected one JSON line of at most 16 KiB")
            request = loads(raw)
            if not isinstance(request, dict):
                raise Fault("invalid_request", "Expected an object")
            actor = text(request.pop("actor", "local:" + str(uid)), 100)
            if request.get("action") in ("watch", "ros_watch", "resource_watch"):
                is_ros = request["action"] == "ros_watch"
                is_resource = request["action"] == "resource_watch"
                fields(request, ("version", "action", "topics") if is_ros else ("version", "action"),
                       ("updates",) if is_ros else ())
                if is_ros and type(request.get("updates", False)) is not bool:
                    raise Fault("invalid_request", "updates must be a boolean")
                if type(request["version"]) is not int or request["version"] != 1:
                    raise Fault("protocol_version", "Expected IPC version 1")
                if is_resource:
                    channel = dispatcher.resources.add()
                    remove = dispatcher.resources.remove
                elif is_ros:
                    channel = dispatcher.call(lambda: dispatcher.core.ros.add(request["topics"], updates=request.get("updates", False)))
                    remove = dispatcher.core.ros.remove
                else:
                    channel = dispatcher.call(dispatcher.hub.add)
                    remove = dispatcher.hub.queues.discard
                try:
                    if is_resource:
                        self.send(dict(ok=True, result=dict(type="snapshot", status=dispatcher.resources.snapshot(history=True))))
                    while not dispatcher.closed.is_set():
                        try:
                            status = channel.get(timeout=5)
                            if is_ros and request.get("updates"):
                                status = dispatcher.call(lambda: dispatcher.core.ros.take_updates(channel))
                            value = dict(type="snapshot", status=status)
                        except queue.Empty:
                            value = dict(type="heartbeat")
                        if select.select([self.request], [], [], 0)[0]:
                            # EOF or unexpected trailing data closes a watch.
                            # This also releases silent ROS demand within 5 s.
                            return
                        self.send(dict(ok=True, result=value))
                finally:
                    if is_resource:
                        remove(channel)
                    else:
                        GLib.idle_add(lambda: (remove(channel), False)[1])
                return
            if request.get("action") == "resource_status":
                fields(request, ("version", "action"), ("history",))
                if type(request["version"]) is not int or request["version"] != 1:
                    raise Fault("protocol_version", "Expected IPC version 1")
                if type(request.get("history", False)) is not bool:
                    raise Fault("invalid_request", "history must be a boolean")
                result = dispatcher.resources.snapshot(history=request.get("history", False))
            elif request.get("action") == "ros_snapshot":
                if not self.server.snapshot_slots.acquire(blocking=False):
                    raise Fault("busy", "Snapshot viewer limit reached")
                snapshots = dispatcher.core.ros.snapshots
                job = channel = None
                try:
                    job, channel = dispatcher.call(lambda: snapshots.add(request))
                    while not dispatcher.closed.is_set():
                        if select.select([self.request], [], [], 0)[0]:
                            return
                        try:
                            value = channel.get(timeout=0.2)
                            break
                        except queue.Empty:
                            continue
                    else:
                        return
                    result = dispatcher.call(lambda: snapshots.validate_result(job, value))
                finally:
                    if job is not None:
                        GLib.idle_add(lambda: (snapshots.remove(job, channel), False)[1])
                    self.server.snapshot_slots.release()
            elif request.get("action") == "logs":
                fields(request, ("version", "action", "run_id"), ("limit", "cursor"))
                target = dispatcher.call(lambda: dispatcher.core.handle(dict(version=request["version"], action="log_target", run_id=request["run_id"])))
                result = journal.read(target, limit=request.get("limit", 100), cursor=request.get("cursor"))
            else:
                result = dispatcher.call(lambda: dispatcher.core.handle(request, actor))
            self.send(dict(ok=True, result=result))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except Fault as exc:
            self.send(dict(ok=False, error=exc.as_dict()))
        except Exception:
            self.send(dict(ok=False, error=dict(code="internal_error", message="Manager request failed; inspect its journal")))


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def __init__(self, address, dispatcher):
        self.dispatcher = dispatcher
        self.slots = threading.BoundedSemaphore(16)
        self.snapshot_slots = threading.BoundedSemaphore(5)
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


def run(profile_path, state):
    os.umask(0o077)
    config, revision = profile.read(profile_path)
    DBusGMainLoop(set_as_default=True)
    store = Store(state)
    core = Core(config, revision, store, Path("/proc/sys/kernel/random/boot_id").read_text().strip())
    ros = core.ros = RosRuntime(core)
    dispatcher = Dispatcher(core)
    resources = dispatcher.resources = ResourceMonitor()
    loop = GLib.MainLoop()
    disconnected = True
    pending = False
    fast_timer = False

    def lost():
        nonlocal disconnected
        disconnected = True
        core.disconnected()

    def audit(full=False):
        nonlocal pending
        pending = False
        try:
            core.audit(full=full)
        except Exception as exc:
            print("Supervisor audit failed:", type(exc).__name__, file=sys.stderr, flush=True)
            lost()
        return GLib.SOURCE_REMOVE

    def changed(_name):
        nonlocal pending
        if not pending:
            pending = True
            GLib.timeout_add(20, lambda: audit(full=True))

    def reconnect():
        nonlocal disconnected
        if disconnected:
            if core.supervisor:
                try:
                    core.supervisor.close()
                except Exception:
                    pass
                core.supervisor = None
            try:
                supervisor = Systemd(core.prefix, changed, lost)
                core.connected(supervisor)
                disconnected = False
            except Exception as exc:
                print("Supervisor unavailable:", type(exc).__name__, file=sys.stderr, flush=True)
        return GLib.SOURCE_CONTINUE

    def periodic():
        if not disconnected:
            audit()
        return GLib.SOURCE_CONTINUE

    def progress():
        nonlocal fast_timer
        if core.needs_progress():
            if disconnected:
                core.progress()
            else:
                audit()
            return GLib.SOURCE_CONTINUE
        fast_timer = False
        return GLib.SOURCE_REMOVE

    def publish():
        nonlocal fast_timer
        dispatcher.hub.publish()
        status = core.status()
        targets = []
        for item in status["units"]:
            run = core.current(item["unit_id"])
            observed = core.observations.get(item["run_id"])
            cgroup = observed.get("ControlGroup") if run and run["blocked"] and owns(run, observed) else None
            targets.append(dict(unit_id=item["unit_id"], label=item["label"], run_id=item["run_id"],
                                lifecycle=item["lifecycle"], cgroup=cgroup))
        resources.update_context(dict(units=targets, fresh=status["fresh"], observed=status["observed_monotonic"]))
        ros.runtime_changed()
        if not fast_timer and core.needs_progress():
            fast_timer = True
            GLib.timeout_add(250, progress)

    core.on_change = publish
    path = store.directory / "runtime.sock"
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_socket():
            raise Fault("unsafe_state", "Refusing to replace a non-socket runtime.sock")
        path.unlink()  # Holding the manager lock proves this is a stale socket.
    server = Server(str(path), dispatcher)
    os.chmod(path, 0o600)
    thread = threading.Thread(target=server.serve_forever, kwargs=dict(poll_interval=0.5), daemon=True)
    thread.start()
    reconnect()
    if ros.config:
        ros.snapshots.runner = SnapshotProcess(ros.config, store.directory, GLib.idle_add)
        ros.transport = RosProcess(ros.config, store.directory, ros.event, GLib.idle_add)
        ros.start()
        GLib.timeout_add(250, lambda: (ros.tick(), GLib.SOURCE_CONTINUE)[1])
    GLib.timeout_add_seconds(5, reconnect)
    GLib.timeout_add_seconds(10, periodic)
    for number in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, number, lambda: (loop.quit(), False)[1])
    try:
        loop.run()
    finally:
        dispatcher.closed.set()
        resources.close()
        ros.snapshots.close()
        ros.close()
        server.shutdown()
        server.server_close()
        if core.supervisor:
            core.supervisor.close()
        path.unlink(missing_ok=True)
        store.close()


def main():
    parser = argparse.ArgumentParser(description="Run the local robot runtime manager; starts no workloads")
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    args = parser.parse_args()
    try:
        run(args.profile, args.state)
    except (Fault, OSError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
