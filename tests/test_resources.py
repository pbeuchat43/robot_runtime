"""Resource arithmetic, attribution, bounded retention and shared demand."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from robot_runtime.common import Fault, dumps
from robot_runtime.ipc import MAX_RESPONSE
from robot_runtime.resource_monitor import LinuxSampler, ResourceMonitor, gpu_values
from robot_runtime.resource_web import ResourceFanout


class FakeGPU:
    def start(self):
        pass

    def close(self):
        pass

    def sample(self, now):
        return dict(activity_percent=25, temperature_c=42)


class SamplerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.cg = self.root / "cgroups"
        self.group = self.cg / "owned"
        self.group.mkdir(parents=True)
        self.write(self.proc / "stat", "cpu 100 0 0 100 0 0 0 0 40 0\n")
        self.write(self.proc / "meminfo", "MemTotal: 1000 kB\nMemAvailable: 600 kB\nSwapTotal: 100 kB\nSwapFree: 80 kB\n")
        self.write(self.group / "cpu.stat", "usage_usec 1000000\n")
        self.write(self.group / "memory.current", "10000\n")
        self.write(self.group / "cgroup.procs", "123\n")
        self.pid = self.proc / "123"
        self.pid.mkdir()
        self.write(self.pid / "stat", self.stat(10, 20))
        self.write(self.pid / "cgroup", "0::/owned\n")
        self.sampler = LinuxSampler(self.proc, self.cg, FakeGPU())
        self.target = dict(unit_id="idle", label="Idle", run_id="first", lifecycle="running", cgroup="/owned")

    def write(self, path, value):
        path.write_text(value)

    def stat(self, birth, rss):
        fields = ["0"] * 22
        fields[0], fields[19], fields[21] = "S", str(birth), str(rss)
        return "123 (name with ) spaces) " + " ".join(fields)

    def sample(self, now, fresh=True):
        return self.sampler.sample(dict(fresh=fresh, observed=now, units=[self.target]), now)

    def test_host_and_descendant_accounting_with_distinct_cpu_denominators(self):
        first = self.sample(100)
        self.assertIsNone(first["host"]["cpu_percent"])
        self.assertIsNone(first["units"][0]["cpu_percent"])
        self.write(self.proc / "stat", "cpu 125 0 0 175 0 0 0 0 50 0\n")
        self.write(self.group / "cpu.stat", "usage_usec 2500000\n")
        child = self.group / "child"
        child.mkdir()
        self.write(child / "cgroup.procs", "123\n")  # Deduplicate a moving PID.
        result = self.sample(101)
        self.assertEqual(result["host"]["cpu_percent"], 25)
        self.assertEqual(result["host"]["memory_used_bytes"], 400 * 1024)
        self.assertEqual(result["host"]["swap_used_bytes"], 20 * 1024)
        self.assertEqual(result["units"][0]["cpu_percent"], 150)
        self.assertEqual(result["units"][0]["rss_bytes"], 20 * os.sysconf("SC_PAGE_SIZE"))
        self.assertEqual(result["units"][0]["memory_current_bytes"], 10000)

    def test_resets_missing_sources_and_replaced_runs_do_not_invent_zero(self):
        self.sample(100)
        self.write(self.group / "cpu.stat", "usage_usec 10\n")
        self.write(self.proc / "stat", "cpu 1 0 0 1 0 0 0 0\n")
        result = self.sample(101)
        self.assertIsNone(result["host"]["cpu_percent"])
        self.assertIsNone(result["units"][0]["cpu_percent"])
        self.target["run_id"] = "replacement"
        self.assertIsNone(self.sample(102)["units"][0]["cpu_percent"])
        (self.group / "cpu.stat").unlink()
        result = self.sample(103)["units"][0]
        self.assertIsNone(result["cpu_percent"])
        self.assertEqual(result["memory_current_bytes"], 10000)
        self.assertTrue(result["errors"])
        self.sampler.close()
        self.sampler.start()
        self.assertIsNone(self.sample(104)["host"]["cpu_percent"])

    def test_stale_or_unowned_targets_and_pid_reuse_are_unavailable(self):
        self.assertIsNone(self.sample(100, fresh=False)["units"][0]["rss_bytes"])
        result = self.sampler.sample(dict(fresh=True, observed=80, units=[self.target]), 100)
        self.assertIsNone(result["units"][0]["rss_bytes"])
        self.write(self.pid / "cgroup", "0::/unrelated\n")
        self.assertIsNone(self.sample(100)["units"][0]["rss_bytes"])
        self.target["cgroup"] = "/../outside"
        self.assertIsNone(self.sample(101)["units"][0]["memory_current_bytes"])
        self.target["cgroup"] = None
        self.assertIsNone(self.sample(102)["units"][0]["cpu_percent"])

    def test_bounded_traversal_and_gpu_partial_values(self):
        self.write(self.group / "cgroup.procs", " ".join(map(str, range(257))))
        self.assertIsNone(self.sample(100)["units"][0]["rss_bytes"])
        self.assertEqual(gpu_values("RAM 42/100MB GR3D_FREQ 99%@[918,0] GPU@54.25C"), dict(activity_percent=99, temperature_c=54.25))
        self.assertEqual(gpu_values("GR3D_FREQ 0% GPU@-1.5C"), dict(activity_percent=0, temperature_c=-1.5))
        self.assertEqual(gpu_values("GR3D_FREQ 0% gpu@54.843C/54.875C"), dict(activity_percent=0, temperature_c=54.843))
        self.assertIsNone(gpu_values("GR3D_FREQ 0% gpu@-256C")["temperature_c"])
        self.assertEqual(gpu_values("GR3D_FREQ 101%"), dict(activity_percent=None, temperature_c=None))


class MonitorTests(unittest.TestCase):
    def test_retention_expiry_size_and_restart_identity(self):
        now = [0.0]
        monitor = ResourceMonitor(clock=lambda: now[0], wall=lambda: now[0])
        value = dict(host=dict(cpu_percent=25.123, memory_used_bytes=123456789, memory_total_bytes=987654321),
                     gpu=dict(activity_percent=70), units=[])
        for index in range(10000):
            now[0] = index / 10
            monitor.record(value, now[0])
        snapshot = monitor.snapshot(history=True)
        self.assertEqual(len(snapshot["history"]), 600)
        self.assertLess(len(dumps(snapshot).encode()), MAX_RESPONSE)
        self.assertFalse(snapshot["fresh"])
        now[0] += 601
        self.assertEqual(monitor.snapshot(history=True)["history"], [])
        self.assertIsNone(monitor.snapshot()["latest"])
        self.assertNotEqual(monitor.generation, ResourceMonitor().generation)

    def test_one_sampler_for_five_viewers_stops_and_resets_on_resume(self):
        class Sampler:
            starts = stops = calls = 0
            def start(self): self.starts += 1
            def close(self): self.stops += 1
            def sample(self, context, now):
                self.calls += 1
                return dict(host={}, gpu={}, units=[])
        sampler = Sampler()
        monitor = ResourceMonitor(sampler)
        self.addCleanup(monitor.close)
        self.assertEqual(sampler.calls, 0)
        with patch("robot_runtime.resource_monitor.INTERVAL", .02):
            channels = [monitor.add() for _ in range(5)]
            deadline = time.monotonic() + 2
            while sampler.calls < 3 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertGreaterEqual(sampler.calls, 3)
            self.assertEqual(sampler.starts, 1)
            self.assertEqual(monitor.snapshot()["consumers"], 5)
            self.assertTrue(all(channel.qsize() == 1 for channel in channels))
            for channel in channels: monitor.remove(channel)
            time.sleep(.1)
            count = sampler.calls
            time.sleep(.1)
            self.assertEqual(sampler.calls, count)
            self.assertFalse(monitor.snapshot()["fresh"])
            channel = monitor.add()
            deadline = time.monotonic() + 2
            while sampler.starts < 2 and time.monotonic() < deadline: time.sleep(.01)
            self.assertEqual(sampler.starts, 2)
            for _ in range(7): monitor.add()
            with self.assertRaises(Fault): monitor.add()
        monitor.close()
        self.assertFalse(monitor.thread.is_alive())


class FanoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_upstream_release_reconnect_history_and_staleness(self):
        fanout = ResourceFanout("/unused")
        entered = 0
        async def upstream():
            nonlocal entered
            entered += 1
            await asyncio.Event().wait()
        fanout.upstream = upstream
        channels = [await fanout.add() for _ in range(5)]
        await asyncio.sleep(0)
        self.assertEqual(entered, 1)
        value = dict(generation="one", age_seconds=0, fresh=True, stale_after_seconds=3,
                     latest=dict(sequence=2, monotonic=100, time_ms=100000, host={}, gpu={}),
                     history=[[1, 99, 99000, 2, 3, 4]])
        fanout.publish(value)
        self.assertEqual(len(fanout.snapshot(history=True)["history"]), 2)
        self.assertNotIn("history", fanout.snapshot())
        fanout.received -= 4
        self.assertFalse(fanout.snapshot()["fresh"])
        revision = fanout.stream_revision
        fanout.publish(dict(value, generation="two", history=[]))
        self.assertGreater(fanout.stream_revision, revision)
        self.assertEqual(len(fanout.history), 1)
        for channel in channels: await fanout.remove(channel)
        self.assertIsNone(fanout.task)
        self.assertFalse(fanout.history)
        await fanout.close()
