"""Latest-only topic delivery and independent freshness, without ROS/hardware."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import json
import time
import unittest

from robot_runtime.ros_web import TopicFanout


class DeliveryTests(unittest.TestCase):
    def test_independent_updates_metadata_reset_and_source_loss(self):
        fanout = TopicFanout("/unused")
        at = time.monotonic()
        def sample(key, sequence):
            return dict(id=key, sequence=sequence, run_id="run", stream_generation="stream",
                        data=dict(data=str(sequence)), status="current", fresh=True, age_seconds=0)
        fanout.apply(dict(reset=True, available=True, sampled_monotonic=at,
            metadata=[dict(id=key, stale_after_seconds=1, fields={"data": "int64"}) for key in ("a", "b")],
            topics=[sample("a", 1), sample("b", 1)]))
        first, second = {}, {}
        baseline = json.loads(fanout.updates(["a", "b"], first))
        self.assertTrue(baseline["reset"])
        self.assertEqual(len(baseline["metadata"]), 2)
        self.assertEqual(len(baseline["updates"]), 2)
        fanout.apply(dict(reset=False, available=True, sampled_monotonic=at + .1, topics=[sample("a", 2)]))
        delta = json.loads(fanout.updates(["a", "b"], first))
        self.assertFalse(delta["reset"])
        self.assertEqual(delta["metadata"], [])
        self.assertEqual([entry["value"]["id"] for entry in delta["updates"]], ["a"])
        self.assertEqual(fanout.values["b"][1], at)
        self.assertEqual(len(json.loads(fanout.updates(["a", "b"], second))["updates"]), 2)
        stale = fanout.snapshot(["a", "b"])
        stale.update(available=False, topics=[dict(topic, fresh=False, status="worker_unavailable") for topic in stale["topics"]])
        fanout.publish(stale)
        reset = json.loads(fanout.updates(["a", "b"], first))
        self.assertTrue(reset["reset"])
        self.assertFalse(reset["header"]["available"])
        self.assertTrue(all(not entry["value"]["fresh"] for entry in reset["updates"]))
        self.assertEqual(len(fanout.records), 2)
