"""Real OpenCV conversion of bounded synthetic ROS image-shaped samples."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
from types import SimpleNamespace
import unittest
import numpy as np
from robot_runtime.av.ros_source import image_to_nv12, frame_deadline


class ImageTests(unittest.TestCase):
    def test_jittered_native_rate_and_bursts_have_bounded_admission(self):
        accepted=[]; deadline=0
        for index in range(300):
            now=10+index/30+(.003 if index%2 else -.003)
            next_at=frame_deadline(now,deadline,30)
            if next_at is not None: accepted.append(now); deadline=next_at
        self.assertGreaterEqual(len(accepted),295)
        accepted=[]; deadline=0
        for index in range(1001):
            now=100+index/1000
            next_at=frame_deadline(now,deadline,30)
            if next_at is not None: accepted.append(now); deadline=next_at
        self.assertLessEqual(len(accepted),32)  # At most one frame of burst credit.
        deadline=frame_deadline(1000,deadline,30)
        self.assertIsNotNone(deadline)
        deadline=frame_deadline(1000.001,deadline,30)
        self.assertIsNone(frame_deadline(1000.002,deadline,30))

    def test_square_and_rectangle_keep_aspect_and_stride(self):
        for width, height, encoding, channels in ((640, 640, "bgr8", 3), (640, 480, "rgb8", 3), (640, 640, "mono8", 1)):
            step = width * channels + 8
            data = np.full((height, step), 255, dtype=np.uint8)
            data[:, width * channels:] = 0
            message = SimpleNamespace(width=width, height=height, step=step, encoding=encoding, data=data.tobytes())
            converted = image_to_nv12(message, 640, 480)
            self.assertEqual(len(converted), 640 * 480 * 3 // 2)
            luma = converted[:640 * 480].reshape(480, 640)
            self.assertEqual(int(luma[240, 320]), 235)
            self.assertEqual(int(luma[240, 0]), 16 if height == 640 else 235)
            if height == 640:
                self.assertEqual(int(luma[240, 79]), 16)
                self.assertEqual(int(luma[240, 80]), 235)

    def test_malformed_payload_rejected(self):
        message = SimpleNamespace(width=640, height=480, step=1920, encoding="bgr8", data=b"short")
        with self.assertRaises(ValueError): image_to_nv12(message, 640, 480)
