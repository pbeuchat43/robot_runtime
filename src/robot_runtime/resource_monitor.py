"""Shared, demand-driven Linux resource observations. No supervisor commands."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from .common import Fault

INTERVAL = 1.0
RETENTION = 600
STALE_AFTER = 3.0
HISTORY_COLUMNS = ["sequence", "monotonic", "time_ms", "cpu_percent", "memory_percent", "gpu_percent"]


def read(path):
    with path.open() as stream:
        value = stream.read(65537)
    if len(value) > 65536:
        raise ValueError("Resource source exceeds limit")
    return value


def pairs(path):
    return {parts[0].rstrip(":"): int(parts[1]) for line in read(path).splitlines() if len(parts := line.split()) >= 2}


def gpu_values(line):
    load = re.search(r"\bGR3D_FREQ\s+(\d+)%", line)
    temperature = re.search(r"\bGPU@(-?\d+(?:\.\d+)?)C", line, re.IGNORECASE)
    return dict(activity_percent=int(load[1]) if load and 0 <= int(load[1]) <= 100 else None,
                temperature_c=float(temperature[1]) if temperature and -50 <= float(temperature[1]) <= 200 else None)


class JetsonGPU:
    """Own only this tegrastats child; never use its global --stop option."""
    def __init__(self):
        self.process = None
        self.buffer = b""
        self.latest = None
        self.received = 0
        self.retry_at = 0

    def start(self):
        self.retry_at = time.monotonic() + 10
        executable = shutil.which("tegrastats")
        if executable:
            try:
                self.process = subprocess.Popen([executable, "--interval", "1000"], stdout=subprocess.PIPE,
                                                stderr=subprocess.DEVNULL, close_fds=True)
                os.set_blocking(self.process.stdout.fileno(), False)
            except OSError:
                self.close()

    def sample(self, now):
        result = dict(activity_percent=None, temperature_c=None, source="tegrastats", error="GPU telemetry unavailable")
        if self.process is not None and self.process.poll() is not None:
            self.close()
        if self.process is None and now >= self.retry_at:
            self.start()
        if self.process is None:
            return result
        try:
            raw = os.read(self.process.stdout.fileno(), 65536)
        except BlockingIOError:
            raw = b""
        except OSError:
            return result
        self.buffer += raw
        if b"\n" in self.buffer:
            lines = self.buffer.split(b"\n")
            self.buffer = lines.pop()
            self.latest = gpu_values(lines[-1].decode("utf-8", errors="replace"))
            self.received = now
        if len(self.buffer) > 65536:
            self.buffer = b""
        if self.latest is not None and now - self.received <= STALE_AFTER:
            result.update(self.latest)
            result["error"] = None if all(value is not None for value in self.latest.values()) else "GPU telemetry incomplete"
        return result

    def close(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=1)
            self.process.stdout.close()
        self.process, self.latest, self.buffer = None, None, b""


class LinuxSampler:
    def __init__(self, proc=Path("/proc"), cgroups=Path("/sys/fs/cgroup"), gpu=None):
        self.proc, self.cgroups = Path(proc), Path(cgroups).resolve()
        self.gpu = gpu if gpu is not None else JetsonGPU()
        self.cpu = None
        self.unit_cpu = {}

    def start(self):
        self.cpu, self.unit_cpu = None, {}
        self.gpu.start()

    def close(self):
        self.gpu.close()
        self.cpu, self.unit_cpu = None, {}

    def host(self):
        result = dict(cpu_percent=None, memory_used_bytes=None, memory_total_bytes=None,
                      swap_used_bytes=None, swap_total_bytes=None, errors=[])
        try:
            counters = tuple(map(int, read(self.proc / "stat").splitlines()[0].split()[1:9]))
            if len(counters) != 8 or min(counters) < 0:
                raise ValueError("Invalid CPU counters")
            total, idle = sum(counters), counters[3] + counters[4]
            if self.cpu is not None:
                delta, idle_delta = total - self.cpu[0], idle - self.cpu[1]
                if delta > 0 and 0 <= idle_delta <= delta:
                    result["cpu_percent"] = round(100 * (delta - idle_delta) / delta, 2)
            self.cpu = total, idle
        except (OSError, ValueError, IndexError):
            self.cpu = None
            result["errors"].append("CPU counters unavailable")
        try:
            values = pairs(self.proc / "meminfo")
            for prefix, total_key, free_key in (("memory", "MemTotal", "MemAvailable"), ("swap", "SwapTotal", "SwapFree")):
                total, free = values[total_key], values[free_key]
                if not 0 <= free <= total:
                    raise ValueError("Invalid memory counters")
                result[prefix + "_total_bytes"] = total * 1024
                result[prefix + "_used_bytes"] = (total - free) * 1024
        except (OSError, ValueError, KeyError):
            result["errors"].append("Memory counters unavailable")
        return result

    def rss(self, directory):
        # Traverse only the selected cgroup, with explicit process/group bounds.
        groups, pids, pending = 0, set(), [directory]
        while pending:
            group = pending.pop()
            groups += 1
            if groups > 64:
                raise ValueError("Cgroup traversal limit")
            pids.update(int(pid) for pid in read(group / "cgroup.procs").split())
            if len(pids) > 256:
                raise ValueError("Cgroup process limit")
            with os.scandir(group) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                        if groups + len(pending) > 64:
                            raise ValueError("Cgroup traversal limit")
        total = 0
        for pid in pids:
            path = self.proc / str(pid)
            try:
                before = read(path / "stat").rsplit(")", 1)[1].split()
                membership = read(path / "cgroup").splitlines()
                cg = next(line[3:] for line in membership if line.startswith("0::"))
                actual = (self.cgroups / cg.lstrip("/")).resolve()
                after = read(path / "stat").rsplit(")", 1)[1].split()
                if before[19] != after[19] or not actual.is_relative_to(directory):
                    raise ValueError("Process membership changed during sample")
                total += max(0, int(after[21])) * os.sysconf("SC_PAGE_SIZE")
            except FileNotFoundError:
                # A process that exited during this approximate RSS snapshot.
                continue
        return total

    def unit(self, target, now, valid):
        result = {key: target[key] for key in ("unit_id", "label", "run_id", "lifecycle")}
        result.update(cpu_percent=None, rss_bytes=None, memory_current_bytes=None, errors=[])
        if not valid or not target.get("cgroup"):
            result["errors"] = ["Supervisor observation unavailable" if not valid else "No owned active cgroup"]
            return result, None
        try:
            directory = (self.cgroups / target["cgroup"].lstrip("/")).resolve()
            if directory == self.cgroups or not directory.is_relative_to(self.cgroups):
                raise ValueError("Invalid cgroup")
            identity = directory.stat().st_ino
        except (OSError, ValueError):
            result["errors"].append("Cgroup unavailable")
            return result, None
        baseline = None
        try:
            usage = pairs(directory / "cpu.stat")["usage_usec"]
            if usage < 0:
                raise ValueError("Invalid CPU counter")
            key = (target["run_id"], identity)
            previous = self.unit_cpu.get(key)
            if previous and now > previous[0] and usage >= previous[1]:
                result["cpu_percent"] = round((usage - previous[1]) / (now - previous[0]) / 10000, 2)
            baseline = (key, (now, usage))
        except (OSError, ValueError, KeyError):
            result["errors"].append("Cgroup CPU unavailable")
        try:
            charged = int(read(directory / "memory.current"))
            if charged < 0:
                raise ValueError("Invalid memory counter")
            result["memory_current_bytes"] = charged
        except (OSError, ValueError):
            result["errors"].append("Cgroup memory unavailable")
        try:
            result["rss_bytes"] = self.rss(directory)
        except (OSError, ValueError, IndexError, StopIteration):
            result["errors"].append("Process RSS unavailable or traversal limit reached")
        return result, baseline

    def sample(self, context, now):
        valid = context.get("fresh", False) and now - context.get("observed", 0) <= 15
        units, baselines = [], {}
        for target in context.get("units", []):
            value, baseline = self.unit(target, now, valid)
            units.append(value)
            if baseline:
                baselines[baseline[0]] = baseline[1]
        self.unit_cpu = baselines
        return dict(host=self.host(), gpu=self.gpu.sample(now), units=units, error=None)


class ResourceMonitor:
    def __init__(self, sampler=None, *, clock=time.monotonic, wall=time.time):
        self.sampler = sampler if sampler is not None else LinuxSampler()
        self.clock, self.wall = clock, wall
        self.generation = uuid.uuid4().hex
        self.condition = threading.Condition()
        self.viewers = set()
        self.context = {}
        self.history = deque(maxlen=RETENTION)
        self.latest = None
        self.sequence = 0
        self.epoch = 0
        self.sample_epoch = None
        self.closed = False
        self.thread = None

    def update_context(self, context):
        with self.condition:
            self.context = context

    def snapshot(self, history=False):
        with self.condition:
            now = self.clock()
            while self.history and now - self.history[0][1] >= RETENTION:
                self.history.popleft()
            if self.latest and now - self.latest["monotonic"] >= RETENTION:
                self.latest = None
            age = max(0, now - self.latest["monotonic"]) if self.latest else None
            result = dict(api_version=1, generation=self.generation, collecting=bool(self.viewers),
                          consumers=len(self.viewers), interval_seconds=INTERVAL, retention_seconds=RETENTION,
                          stale_after_seconds=STALE_AFTER, age_seconds=age, fresh=bool(self.viewers and self.sample_epoch == self.epoch and age is not None and age <= STALE_AFTER),
                          latest=self.latest, sequence=self.sequence, sample_count=len(self.history),
                          server_time_ms=int(self.wall() * 1000))
            if history:
                result.update(history=list(self.history), history_columns=HISTORY_COLUMNS)
            return result

    def add(self):
        with self.condition:
            if self.closed or len(self.viewers) >= 8:
                raise Fault("busy", "Resource observation connection limit reached")
            channel = queue.Queue(maxsize=1)
            if not self.viewers:
                self.epoch += 1
            self.viewers.add(channel)
            channel.put_nowait(self.snapshot())
            if self.thread is None:
                self.thread = threading.Thread(target=self.run, name="resource-monitor", daemon=True)
                self.thread.start()
            self.condition.notify_all()
            return channel

    def remove(self, channel):
        with self.condition:
            self.viewers.discard(channel)
            self.condition.notify_all()

    def record(self, value, now):
        # Caller holds the condition. Also used by deterministic retention tests.
        self.sequence += 1
        self.sample_epoch = self.epoch
        self.latest = dict(value, sequence=self.sequence, monotonic=now, time_ms=int(self.wall() * 1000))
        host, gpu = value.get("host", {}), value.get("gpu", {})
        memory = 100 * host["memory_used_bytes"] / host["memory_total_bytes"] if host.get("memory_total_bytes") and host.get("memory_used_bytes") is not None else None
        self.history.append([self.sequence, now, self.latest["time_ms"], host.get("cpu_percent"), memory, gpu.get("activity_percent")])
        snapshot = self.snapshot()
        for channel in self.viewers:
            try:
                channel.get_nowait()
            except queue.Empty:
                pass
            channel.put_nowait(snapshot)

    def run(self):
        active_epoch = None
        try:
            while True:
                with self.condition:
                    if self.closed:
                        return
                    context, epoch, wanted = self.context, self.epoch, bool(self.viewers)
                if not wanted:
                    if active_epoch is not None:
                        self.sampler.close()
                        active_epoch = None
                    with self.condition:
                        self.condition.wait_for(lambda: self.closed or bool(self.viewers))
                    continue
                if active_epoch != epoch:
                    self.sampler.close()
                    self.sampler.start()
                    active_epoch = epoch
                now = self.clock()
                try:
                    value = self.sampler.sample(context, now)
                except Exception:
                    value = dict(host={}, gpu={}, units=[], error="Resource sampling failed")
                with self.condition:
                    if self.viewers and epoch == self.epoch:
                        self.record(value, now)
                    self.condition.wait_for(lambda: self.closed or not self.viewers or epoch != self.epoch,
                                            timeout=max(0, INTERVAL - (self.clock() - now)))
        finally:
            self.sampler.close()

    def close(self):
        with self.condition:
            self.closed = True
            self.viewers.clear()
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout=4)
