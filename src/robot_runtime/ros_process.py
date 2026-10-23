"""Bounded pipes to an explicitly configured ROS-native interpreter."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import os
import queue
import subprocess
import threading
from pathlib import Path

from .common import Fault, dumps, loads
from .ipc import MAX_RESPONSE


def worker_launch(config, state, filename):
    environment = dict(PATH="/usr/bin:/bin", HOME=str(Path.home()), LANG="C.UTF-8", PYTHONNOUSERSITE="1",
                       PYTHONUNBUFFERED="1", ROS_DOMAIN_ID=str(config["domain_id"]),
                       ROS_AUTOMATIC_DISCOVERY_RANGE="LOCALHOST", ROS_STATIC_PEERS="",
                       ROS_LOG_DIR=str(state / "ros-logs"), RCUTILS_LOGGING_USE_STDOUT="0")
    # Trusted setup paths are separate argv entries, never shell source text.
    script = 'count="$1"; shift; for ((i=0;i<count;i++)); do source "$1" >&2 || exit; shift; done; exec "$@"'
    argv = ["/bin/bash", "--noprofile", "--norc", "-c", script, "robot-runtime-ros",
            str(len(config["setup_scripts"])), *config["setup_scripts"], config["python"],
            str(Path(__file__).with_name(filename))]
    return argv, environment


class RosProcess:
    def __init__(self, config, state, callback, schedule):
        self.config, self.state, self.callback, self.schedule = config, state, callback, schedule
        self.process = None
        self.outgoing = None

    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self, generation):
        if self.running():
            raise Fault("busy", "ROS worker is already running")
        argv, environment = worker_launch(self.config, self.state, "ros_worker.py")
        process = subprocess.Popen(argv, cwd=self.state, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=None)
        self.process = process
        outgoing = self.outgoing = queue.Queue(maxsize=8)
        incoming = queue.Queue(maxsize=24)
        scheduled = threading.Event()

        def deliver():
            # Keep the inbox bounded while the manager is doing a supervisor audit.
            while True:
                try:
                    value = incoming.get_nowait()
                except queue.Empty:
                    scheduled.clear()
                    # The reader may have raced with clearing the notification.
                    if not incoming.empty() and not scheduled.is_set():
                        scheduled.set()
                        continue
                    return False
                self.callback(value)

        def enqueue(value):
            while process.poll() is None or value.get("kind") == "lost":
                try:
                    incoming.put(value, timeout=0.2)
                    if not scheduled.is_set():
                        scheduled.set()
                        self.schedule(deliver)
                    return
                except queue.Full:
                    if process.poll() is not None:
                        return

        def read():
            try:
                while True:
                    raw = process.stdout.readline(MAX_RESPONSE + 1)
                    if not raw or len(raw) > MAX_RESPONSE or not raw.endswith(b"\n"):
                        break
                    value = loads(raw)
                    if not isinstance(value, dict) or value.get("generation") != generation:
                        break
                    enqueue(value)
            except (OSError, Fault):
                pass
            finally:
                enqueue(dict(kind="lost", generation=generation))
                if process.poll() is None:
                    process.terminate()
                process.stdout.close()

        def write():
            try:
                while process.poll() is None:
                    try:
                        raw = outgoing.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    view = memoryview(raw)
                    while view:
                        count = process.stdin.write(view)
                        if not count:
                            raise BrokenPipeError()
                        view = view[count:]
                    process.stdin.flush()
            except OSError:
                pass
            finally:
                process.stdin.close()

        threading.Thread(target=read, daemon=True).start()
        threading.Thread(target=write, daemon=True).start()
        self.send(dict(kind="init", config=self.config, state=str(self.state), generation=generation))

    def send(self, value):
        if not self.running():
            raise Fault("ros_unavailable", "ROS worker is unavailable")
        raw = (dumps(value) + "\n").encode()
        if len(raw) > MAX_RESPONSE:
            raise Fault("request_limit", "ROS worker request exceeds its byte budget")
        try:
            self.outgoing.put_nowait(raw)
        except queue.Full as exc:
            raise Fault("busy", "ROS worker input is full") from exc

    def close(self):
        if self.running():
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                # This private management helper is not an application workload.
                # Its death cannot cancel or roll back a remote ROS request.
                self.process.kill()
                self.process.wait(timeout=2)
