"""IMU meaning, field availability and expiry without ROS or hardware."""
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
from imu_fixture import make_profile


class ImuTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = make_profile(Path(self.temporary.name), "unit_test")
        self.spec, self.health, _peer = self.config["ros"]["topics"]
        self.message = Record(header=Record(stamp=Record(sec=1791637000, nanosec=123456789), frame_id="sensor"),
            orientation=Record(x=0., y=0., z=0., w=.9999), angular_velocity=Record(x=1., y=2., z=3.),
            linear_acceleration=Record(x=0., y=0., z=9.81), orientation_covariance=[0.]*9,
            angular_velocity_covariance=[0.]*9, linear_acceleration_covariance=[0.]*9)

    def test_sample_time_is_lossless_receipt_estimate_and_quaternion_remains_raw(self):
        value = project_observation(self.message, self.spec)
        self.assertEqual(value["sample_timestamp_ns"], "1791637000123456789")
        self.assertNotIn("acquisition_timestamp_ns", value)
        self.assertEqual(value["timestamp_semantics"], "estimated_mcu_callback_receipt")
        self.assertEqual(value["orientation"]["value"]["w"], .9999)
        self.assertFalse(value["orientation"]["covariance_known"])
        self.assertEqual(value["linear_acceleration"]["value"]["z"], 9.81)
        self.assertEqual(value["acceleration_semantics"], "specific_force")

    def test_partial_fields_clear_values_instead_of_reusing_or_showing_zero_orientation(self):
        self.message.orientation_covariance[0] = -1.
        self.message.orientation.w = 0.
        value = project_observation(self.message, self.spec)
        self.assertIsNone(value["orientation"])
        self.assertIsNotNone(value["angular_velocity"])
        self.message.angular_velocity_covariance[0] = self.message.linear_acceleration_covariance[0] = -1.
        with self.assertRaises(Fault) as caught:
            project_observation(self.message, self.spec)
        self.assertEqual(caught.exception.code, "measurement_unavailable")

    def test_invalid_quaternion_frame_stamp_numeric_and_byte_bounds_fail_closed(self):
        for field in ("quaternion", "frame", "stamp", "numeric", "covariance"):
            message = copy.deepcopy(self.message)
            if field == "quaternion": message.orientation.w = 0.
            if field == "frame": message.header.frame_id = "base_link"
            if field == "stamp": message.header.stamp.sec = message.header.stamp.nanosec = 0
            if field == "numeric": message.angular_velocity.x = float("nan")
            if field == "covariance": message.linear_acceleration_covariance[4] = -2.
            with self.assertRaises(Fault):
                project_observation(message, self.spec)
        with self.assertRaises(Fault):
            project_observation(self.message, dict(self.spec, max_json_bytes=64))

    def test_schema_requires_explicit_meaning_and_bounds_the_imu_rate(self):
        validate(self.config, check_paths=False)
        for field, value in (("timestamp_clock", "mcu"), ("timestamp_semantics", "unspecified"), ("acceleration_semantics", "unknown")):
            config = copy.deepcopy(self.config)
            config["ros"]["topics"][0]["projection"][field] = value
            with self.assertRaises(Fault): validate(config, check_paths=False)
        config = copy.deepcopy(self.config)
        config["ros"]["topics"][0]["max_rate_hz"] = 21
        with self.assertRaises(Fault): validate(config, check_paths=False)
        config = copy.deepcopy(self.config)
        config["ros"]["topics"][1]["max_rate_hz"] = 20
        with self.assertRaises(Fault): validate(config, check_paths=False)

    def test_diagnostics_without_a_cumulative_counter_do_not_invent_progress(self):
        item = Record(name="imu", level=b"\1", message="CALIBRATING", values=[Record(key="accuracy", value="low")])
        value = project_observation(Record(status=[item]), self.health)
        self.assertEqual(value["level"], 1)
        self.assertEqual(value["values"], dict(accuracy="low"))
        self.assertNotIn("progress_counter", value)
        item.name = "peer"
        with self.assertRaises(Fault): project_observation(Record(status=[item]), self.health)
