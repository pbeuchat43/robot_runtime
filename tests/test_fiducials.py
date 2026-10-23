"""Physical marker metadata, full fixed pose transform and invalid-pose bounds."""
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
from robot_runtime.ros_observations import observation_schema, project_observation
from fiducials_fixture import make_profile


class FiducialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.profile = make_profile(Path(temporary.name), "test")
        self.spec = self.profile["ros"]["topics"][-1]
        self.item = Record(id=7, marker_size_m=.081, pose_valid=True, reprojection_error_px=.4,
            pose=Record(position=Record(x=-.2, y=-.1, z=1.5), orientation=Record(x=0., y=0., z=0., w=1.)))
        self.message = Record(header=Record(stamp=Record(sec=1791560000,nanosec=123456789),frame_id="camera_optical"),
            dictionary_name="DICT_4X4_50", detections=[self.item, copy.deepcopy(self.item)])

    def test_metadata_duplicates_full_pose_and_acquisition_stamp(self):
        validate(self.profile, check_paths=False)
        value = project_observation(self.message, self.spec)
        self.assertEqual(value["acquisition_timestamp_ns"], "1791560000123456789")
        self.assertEqual(value["dictionary_name"], "DICT_4X4_50")
        self.assertEqual(len(value["detections"]), 2)
        first = value["detections"][0]
        self.assertEqual(first["id"], 7)
        self.assertEqual(first["marker_size_m"], .081)
        for key, expected in zip("xyz", [1.7,.2,.4]): self.assertAlmostEqual(first["position"][key], expected)
        self.assertEqual(first["orientation_xyzw"], (-.5,.5,-.5,.5))
        self.item.pose.orientation = Record(x=0., y=0., z=math.sqrt(.5), w=math.sqrt(.5))
        rotated = project_observation(self.message,self.spec)["detections"][0]["orientation_xyzw"]
        for actual, expected in zip(rotated, [0,math.sqrt(.5),0,math.sqrt(.5)]): self.assertAlmostEqual(actual,expected)

    def test_invalid_pose_is_metadata_only_and_empty_is_a_distinct_sample(self):
        self.item.pose_valid = False
        self.item.reprojection_error_px = float("nan")
        self.item.pose.position.x = float("nan")
        value = project_observation(self.message,self.spec)
        self.assertEqual((value["positioned_count"],value["invalid_pose_count"]), (1,1))
        for key in ("position","orientation_xyzw","reprojection_error_px"): self.assertIsNone(value["detections"][0][key])
        self.assertEqual(value["detections"][0]["id"],7)
        self.message.detections = []
        empty = project_observation(self.message,self.spec)
        self.assertEqual((empty["original_count"],empty["detections"]), (0,[]))

    def test_bounded_output_and_invalid_values_fail_closed(self):
        self.message.detections = [self.item]*25
        value = project_observation(self.message,self.spec)
        self.assertEqual((value["original_count"],value["displayed_count"],value["truncated"]), (25,20,True))
        self.assertLess(len(dumps(value).encode()),16384)
        for field, bad in (("marker_size_m",0.),("reprojection_error_px",-1.),("id",-1)):
            message = copy.deepcopy(self.message)
            setattr(message.detections[0],field,bad)
            with self.assertRaises(Fault): project_observation(message,self.spec)
        self.item.pose.orientation.w = 0.
        with self.assertRaises(Fault): project_observation(self.message,self.spec)
        self.message.header.frame_id = "map"
        with self.assertRaisesRegex(Fault,"frame"): project_observation(self.message,self.spec)

    def test_explicit_mount_clock_and_record_schema_are_required(self):
        for key, bad in (("max_items",21),("quaternion_xyzw",[0,0,0,2]),("acquisition_clock","device"),("id_field","id.nested")):
            config = copy.deepcopy(self.profile)
            config["ros"]["topics"][-1]["projection"][key] = bad
            with self.assertRaises(Fault): validate(config,check_paths=False)
        batch_fields = dict(header="std_msgs/Header",dictionary_name="string",detections="sequence<example/Marker>")
        record_fields = dict(id="int32",marker_size_m="float",pose_valid="boolean",pose="geometry_msgs/Pose",reprojection_error_px="float")
        batch = Record(get_fields_and_field_types=lambda:batch_fields)
        item = Record(get_fields_and_field_types=lambda:record_fields)
        get_message = lambda name: lambda:item
        observation_schema(batch,self.spec,get_message)
        record_fields["pose"] = "geometry_msgs/Point"
        with self.assertRaises(Fault): observation_schema(batch,self.spec,get_message)
