"""Projection bounds and freshness contracts without native ROS."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace as Record
import unittest

from robot_runtime.common import Fault
from robot_runtime.profile import validate
from robot_runtime.ros_observations import project_observation
from observations_fixture import make_profile


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = make_profile(Path(self.temporary.name), "unit_test")
        self.spec, self.health = self.config["ros"]["topics"]
        self.message = Record(header=Record(stamp=Record(sec=1791560000, nanosec=123456789), frame_id="base_link"),
            acquisition_to_publish_latency_s=0.1,
            detections=[Record(position=Record(x=1., y=-2., z=0.3), color=2, classification_confidence=0.8)] * 25)

    def test_lossless_acquisition_xyz_and_explicit_first_twenty_limit(self):
        value = project_observation(self.message, self.spec)
        self.assertEqual(value["acquisition_timestamp_ns"], "1791560000123456789")
        self.assertEqual((value["original_count"], value["displayed_count"], value["truncated"]), (25, 20, True))
        self.assertEqual(value["detections"][0]["z"], 0.3)
        self.message.detections = []
        empty = project_observation(self.message, self.spec)
        self.assertEqual(empty["detections"], [])
        self.assertFalse(empty["truncated"])

    def test_profile_bounds_and_supported_clock_are_explicit(self):
        validate(self.config, check_paths=False)
        for key, value in (("max_items", 21), ("acquisition_clock", "unspecified"), ("forward_m", float("nan")), ("array_field", "x.y")):
            bad = copy.deepcopy(self.config)
            bad["ros"]["topics"][0]["projection"][key] = value
            with self.assertRaises(Fault):
                validate(bad, check_paths=False)
        bad = copy.deepcopy(self.config)
        bad["ros"]["topics"][0]["fields"] = ["data"]
        with self.assertRaises(Fault):
            validate(bad, check_paths=False)

    def test_bad_frame_nonfinite_data_zero_stamp_and_output_limit_fail_closed(self):
        for path, value in (("frame", "map"), ("position", float("nan")), ("stamp", 0), ("confidence", 1.1), ("latency", -1.0)):
            message = copy.deepcopy(self.message)
            if path == "frame": message.header.frame_id = value
            if path == "position": message.detections[0].position.x = value
            if path == "stamp": message.header.stamp.sec = message.header.stamp.nanosec = value
            if path == "confidence": message.detections[0].classification_confidence = value
            if path == "latency": message.acquisition_to_publish_latency_s = value
            with self.assertRaises(Fault):
                project_observation(message, self.spec)
        with self.assertRaises(Fault):
            project_observation(self.message, dict(self.spec, max_json_bytes=64))

    def test_diagnostic_selection_duplicate_rejection_and_key_allowlist(self):
        item = Record(name="Fixture camera", level=b"\0", message="running", values=[Record(key="frames", value="9007199254740993"), Record(key="secret", value="omitted")])
        message = Record(status=[item])
        value = project_observation(message, self.health)
        self.assertEqual(value["values"], dict(frames="9007199254740993"))
        self.assertEqual(value["progress_counter"], "9007199254740993")
        for statuses in ([], [item, item]):
            with self.assertRaises(Fault):
                project_observation(Record(status=statuses), self.health)
        item.values.append(Record(key="frames", value="2"))
        with self.assertRaises(Fault):
            project_observation(message, self.health)
