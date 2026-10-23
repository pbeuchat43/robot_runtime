"""Launch selection, capture lifetime and image boundary regressions (jah only)."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import copy
import tempfile
import unittest
import uuid
from array import array
from pathlib import Path
from types import SimpleNamespace as NS

from robot_runtime.common import Fault
from robot_runtime.core import Core
from robot_runtime.profile import validate
from robot_runtime.ros_runtime import RosRuntime
from robot_runtime.snapshot_image import encode_image
from robot_runtime.store import Store
from test_core import Clock, Supervisor, profile


def snapshot_spec():
    return dict(id="image", label="Test image", unit_id="worker", node="/provider", name="/image",
                type="sensor_msgs/msg/Image", frame_id="camera", acquisition_clock="system", required_mode="images",
                timeout_seconds=4, stale_after_seconds=2, max_serialized_bytes=8388608,
                max_width=640, max_height=480, max_jpeg_bytes=131072, jpeg_quality=80)


def image(width=800, height=600, encoding="bgr8", padding=0):
    channels = 1 if encoding == "mono8" else 3
    step = width * channels + padding
    return NS(header=NS(frame_id="camera", stamp=NS(sec=1000, nanosec=123456789)),
              width=width, height=height, encoding=encoding, step=step, data=array("B", [128]) * (step * height))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.clock = Clock()
        config = profile()
        config["units"][0]["launch_modes"] = [dict(id="images", label="Start with images", argv=["/usr/bin/python3", "-c", "print('images')"])]
        config["ros"] = dict(schema_version=1, python="/usr/bin/python3", setup_scripts=["/opt/ros/jazzy/setup.bash"], domain_id=224,
                             discovery="LOCALHOST", topics=[], services=[], snapshots=[snapshot_spec()])
        self.config, self.revision = validate(config)
        self.core = Core(self.config, self.revision, self.store, "boot", clock=self.clock, wall=self.clock.wall)
        self.supervisor = Supervisor(self.clock)
        self.core.connected(self.supervisor)
        self.core.ros = RosRuntime(self.core)
        self.snapshots = self.core.ros.snapshots
        self.started = []
        self.snapshots.runner = NS(start=lambda job, callback: self.started.append(job))

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def command(self, action="start", **values):
        return dict(version=1, action=action, unit_id="worker", request_id=str(uuid.uuid4()),
                    config_revision=self.revision, expires_at_ms=int(self.clock.wall() * 1000) + 10000, **values)

    def request(self):
        return dict(version=1, action="ros_snapshot", capability_id="image", expected_run=self.core.current("worker")["run_id"],
                    generation=self.core.generation, config_revision=self.revision)

    def test_default_and_declared_launch_survive_manager_recovery(self):
        self.core.handle(self.command())
        ordinary = self.core.current("worker")
        self.assertIsNone(ordinary["launch_mode"])
        self.assertEqual(ordinary["spec"]["argv"], profile()["units"][0]["argv"])
        with self.assertRaisesRegex(Fault, "Images are disabled"):
            self.snapshots.add(self.request())
        with self.assertRaises(Fault):
            self.core.handle(self.command(mode="images"))
        self.supervisor.exit(ordinary)
        self.core.audit(full=True)
        request = self.command(mode="images")
        self.core.handle(request)
        run = self.core.current("worker")
        self.assertEqual(run["spec"]["argv"][-1], "print('images')")
        restarted = Core(self.config, self.revision, self.store, "boot", clock=self.clock, wall=self.clock.wall)
        restarted.connected(self.supervisor)
        public = restarted.status()["units"][0]
        self.assertEqual(public["launch_mode"]["id"], "images")
        self.assertEqual(public["ownership"], "owned")
        self.assertNotIn("argv", public["launch_modes"][0])
        restarted.handle(request)
        self.assertEqual(self.supervisor.starts, 2)
        # Retained request identity still works after a later profile removes a mode.
        restarted.units["worker"]["launch_modes"] = []
        self.assertEqual(restarted.handle(request)["run_id"], run["run_id"])
        self.assertEqual(self.supervisor.starts, 2)

    def test_unknown_modes_and_nonstart_modes_never_dispatch(self):
        for request in (self.command(mode="unknown"), self.command(mode=None), self.command("stop", mode="images", expected_run="x")):
            with self.assertRaises(Fault):
                self.core.handle(request)
        self.assertEqual(self.supervisor.starts, 0)
        self.assertEqual(self.supervisor.signals, 0)
        malformed = copy.deepcopy(self.config)
        malformed["units"][0]["launch_modes"] *= 2
        with self.assertRaises(Fault):
            validate(malformed)
        malformed = copy.deepcopy(self.config)
        malformed["ros"]["snapshots"][0]["required_mode"] = "unknown"
        with self.assertRaises(Fault):
            validate(malformed)

    def test_capture_shares_five_waiters_and_cancels_last_consumer(self):
        self.core.handle(self.command(mode="images"))
        waiters = [self.snapshots.add(self.request()) for _ in range(5)]
        self.assertEqual(len(self.started), 1)
        with self.assertRaises(Fault):
            self.snapshots.add(self.request())
        for job, channel in waiters[:-1]:
            self.snapshots.remove(job, channel)
        self.assertFalse(job["cancel"].is_set())
        self.snapshots.remove(*waiters[-1])
        self.assertTrue(job["cancel"].is_set())
        self.snapshots.finish(job, Fault("cancelled", "Cancelled"))
        with self.assertRaisesRegex(Fault, "Wait one second"):
            self.snapshots.add(self.request())

    def test_results_age_and_are_invalidated_by_stop_or_generation(self):
        self.core.handle(self.command(mode="images"))
        job, channel = self.snapshots.add(self.request())
        result = dict(received_monotonic=self.clock(), acquisition_age_at_receipt=0.5)
        self.assertEqual(self.snapshots.validate_result(job, result)["acquisition_age_seconds"], 0.5)
        self.clock.now += 2
        with self.assertRaisesRegex(Fault, "stale"):
            self.snapshots.validate_result(job, result)
        self.core.handle(self.command("stop", expected_run=job["binding"]["run_id"]))
        self.snapshots.runtime_changed()
        self.assertTrue(job["cancel"].is_set())
        self.assertIsInstance(channel.get_nowait(), Fault)
        with self.assertRaises(Fault):
            self.snapshots.validate_result(job, result)


class ImageTests(unittest.TestCase):
    def encode(self, message, spec=None, wall_ns=1000200000000):
        return encode_image(message, spec or snapshot_spec(), wall_ns=wall_ns, received_monotonic=10)

    def test_dimensions_padding_supported_formats_and_lossless_stamp(self):
        for encoding in ("bgr8", "rgb8", "mono8"):
            value = self.encode(image(encoding=encoding, padding=4))
            self.assertEqual((value["width"], value["height"]), (640, 480))
            self.assertEqual(value["acquisition_timestamp_ns"], "1000123456789")
            self.assertLessEqual(value["jpeg_bytes"], 131072)
        value = self.encode(image(80, 60))
        self.assertEqual((value["width"], value["height"]), (80, 60))

    def test_invalid_frame_time_format_stride_and_output_budget_fail_closed(self):
        cases = []
        for name, value in (("encoding", "16UC1"), ("step", 1), ("width", 0), ("height", 5000), ("data", array("B", [1]))):
            message = image()
            setattr(message, name, value)
            cases.append(message)
        message = image(); message.header.frame_id = "wrong"; cases.append(message)
        message = image(); message.header.stamp.sec = 0; cases.append(message)
        message = image(); message.header.stamp.nanosec = 10**9; cases.append(message)
        for message in cases:
            with self.assertRaises(Fault):
                self.encode(message)
        for wall in (900000000000, 1010000000000):
            with self.assertRaises(Fault):
                self.encode(image(), wall_ns=wall)
        with self.assertRaises(Fault):
            self.encode(image(), dict(snapshot_spec(), max_jpeg_bytes=1024))


if __name__ == "__main__":
    unittest.main()
