"""LaserScan range, geometry, transform and bounded representation contracts."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import copy
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace as Record
import unittest

from robot_runtime.common import Fault, dumps
from robot_runtime.profile import validate
from robot_runtime.ros_observations import project_observation
from lidar_fixture import make_profile


class LidarTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.profile = make_profile(Path(temporary.name), "test")
        self.spec = self.profile["ros"]["topics"][1]
        self.message = Record(header=Record(stamp=Record(sec=1791560000, nanosec=123456789), frame_id="laser"),
            angle_min=0., angle_max=math.pi, angle_increment=math.pi/2, range_min=.15, range_max=12., scan_time=.1, time_increment=.05, ranges=[1.,2.,3.])

    def test_mount_preserves_xyz_ray_indices_and_scan_time_meaning(self):
        validate(self.profile, check_paths=False)
        value = project_observation(self.message, self.spec)
        self.assertEqual(value["points"], [[-.8,0.,.12,0],[.2,-2.,.12,1],[3.2,0.,.12,2]])
        self.assertEqual(value["acquisition_timestamp_ns"], "1791560000123456789")
        self.assertEqual(value["timestamp_semantics"], "estimated_scan_start")
        self.spec["projection"]["quaternion_xyzw"] = [0, math.sqrt(.5), 0, math.sqrt(.5)]
        self.assertEqual(project_observation(self.message, self.spec)["points"][0], [.2,0.,-.88,0])

    def test_invalid_ranges_are_omitted_and_fresh_empty_is_explicit(self):
        for ranges in ([float("inf"),float("nan"),.1], [13.,float("-inf"),0.]):
            self.message.ranges = ranges
            value = project_observation(self.message, self.spec)
            self.assertEqual((value["valid_count"],value["displayed_count"],value["points"]), (0,0,[]))
        self.message.ranges = [.15, 12., float("nan")]
        self.assertEqual(project_observation(self.message,self.spec)["valid_count"], 2)

    def test_downsampling_covers_entire_scan_and_bounds_json(self):
        self.message.ranges = [12.] * 8192
        self.message.angle_increment = 2*math.pi/8192
        self.message.angle_max = 8191*self.message.angle_increment
        value = project_observation(self.message,self.spec)
        self.assertEqual((value["original_count"],value["valid_count"],value["displayed_count"]), (8192,8192,360))
        self.assertEqual((value["points"][0][3],value["points"][-1][3]), (0,8191))
        self.assertTrue(value["downsampled"])
        self.assertLess(len(dumps(value).encode()), 16384)

    def test_bad_metadata_frame_and_output_size_fail_closed(self):
        for key,value in (("angle_max",0.),("angle_increment",float("nan")),("range_min",-1.),("range_max",.1),("scan_time",-1.),("time_increment",float("inf"))):
            message = copy.deepcopy(self.message)
            setattr(message,key,value)
            with self.assertRaises(Fault): project_observation(message,self.spec)
        self.message.header.frame_id = "map"
        with self.assertRaisesRegex(Fault,"frame"): project_observation(self.message,self.spec)
        self.message.header.frame_id = "laser"
        with self.assertRaises(Fault): project_observation(self.message,dict(self.spec,max_json_bytes=64))

    def test_profile_requires_explicit_valid_mount_and_clock(self):
        for key,value in (("max_points",361),("quaternion_xyzw",[0,0,0,2]),("translation_m",[0,0]),("timestamp_clock","device")):
            profile = copy.deepcopy(self.profile)
            profile["ros"]["topics"][1]["projection"][key] = value
            with self.assertRaises(Fault): validate(profile,check_paths=False)
