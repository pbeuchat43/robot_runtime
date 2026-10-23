# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import json
from pathlib import Path
import tempfile
import unittest

from robot_runtime.av.gamepad import GamepadReceipt, validate_packet, compact_ack


def packet(seq=1, lease=None, **values):
    lease = lease or {'epoch': '0' * 32, 'lease': 1}
    return json.dumps(dict(v=2, type='gamepad', seq=seq, sent_ms=100, active=True, enabled=True,
                          epoch=lease['epoch'], lease=lease['lease'], video_age_ms=20,
                          source='gamepad', mapping='standard', axes=[.55, 0, 0, 0],
                          buttons=[0] * 7 + [.7] + [0] * 9) | values)


class ReceiptTests(unittest.TestCase):
    def test_input_only_session_does_not_require_video_but_input_still_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [10.0]
            receiver = GamepadReceipt(Path(directory), clock=lambda: now[0])
            try:
                hello = receiver.open('input', require_video=False)
                ack = receiver.receive('input', packet(1, hello, video_age_ms=None))
                self.assertTrue(ack['active'])
                self.assertFalse(ack['actuators_enabled'])
                now[0] += .251
                self.assertFalse(receiver.snapshot()['input']['fresh'])
                self.assertFalse(receiver.receive('input', packet(2, hello, video_age_ms=None))['accepted'])
            finally:
                receiver.close()

    def test_compact_ack_preserves_receipt_identity_and_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = GamepadReceipt(Path(directory))
            try:
                ack = receipt.receive('viewer', packet(lease=receipt.open('viewer')))
                compact = compact_ack(ack)
                self.assertEqual(compact['seq'], ack['seq'])
                self.assertEqual(compact['preview'], ack['preview'])
                self.assertFalse(compact['actuators_enabled'])
                self.assertNotIn('axes', compact)
                self.assertNotIn('buttons', compact)
            finally:
                receipt.close()

    def test_reorder_expiry_and_disconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [10.0]
            receiver = GamepadReceipt(Path(directory), clock=lambda: now[0])
            try:
                lease = receiver.open('viewer')
                ack = receiver.receive('viewer', packet(2, lease))
                self.assertEqual(ack['preview'], {'steering': .5, 'throttle': .7, 'brake': 0})
                self.assertFalse(ack['actuators_enabled'])
                now[0] += .05
                self.assertIsNone(receiver.receive('viewer', packet(1, lease)))
                self.assertEqual(receiver.snapshot()['viewer']['last_seq'], 2)
                now[0] += .25
                stale = receiver.snapshot()['viewer']
                self.assertFalse(stale['fresh'])
                self.assertEqual(stale['preview']['throttle'], 0)
                receiver.receive('viewer', packet(3, lease, active=False, enabled=False))
                self.assertFalse(receiver.snapshot()['viewer']['fresh'])
                receiver.remove('viewer')
                self.assertEqual(receiver.snapshot(), {})
            finally:
                receiver.close()

    def test_untrusted_values_are_rejected(self):
        for value in [packet(seq=True), packet(axes=[float('nan')]), packet(buttons=[2]),
                      packet(active=1), packet(mapping='unknown'), packet(axes=[0] * 17), '[]', 'x' * 4097]:
            with self.subTest(value=value[:60]), self.assertRaises(ValueError):
                validate_packet(value)

    def test_nonstandard_has_raw_values_but_no_driving_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            receiver = GamepadReceipt(Path(directory))
            try:
                ack = receiver.receive('viewer', packet(lease=receiver.open('viewer'), mapping='', axes=[.8], buttons=[1]))
                self.assertEqual(ack['axes'], [.8])
                self.assertEqual(ack['preview']['steering'], 0)
            finally:
                receiver.close()

    def test_delayed_packet_cannot_restart_freshness_and_new_lease_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [100.0]
            receiver = GamepadReceipt(Path(directory), clock=lambda: now[0])
            try:
                lease = receiver.open('viewer')
                now[0] += 1
                rejected = receiver.receive('viewer', packet(1, lease))
                self.assertFalse(rejected['accepted'])
                self.assertFalse(receiver.snapshot()['viewer']['fresh'])
                self.assertEqual(rejected['preview']['throttle'], 0)
                now[0] += .05
                recovered = receiver.receive('viewer', packet(2, rejected))
                self.assertTrue(recovered['active'])
                self.assertFalse(receiver.paused())
                # Deadline remains tied to the lease issue time, not this receipt.
                now[0] += .201
                self.assertFalse(receiver.snapshot()['viewer']['fresh'])
            finally:
                receiver.close()

    def test_pause_interlock_epoch_and_missing_or_old_video(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [100.0]
            receiver = GamepadReceipt(Path(directory), clock=lambda: now[0])
            try:
                lease = receiver.open('viewer')
                self.assertTrue(receiver.paused())
                ack = receiver.receive('viewer', packet(1, lease), allow_enable=False)
                self.assertFalse(ack['enabled'])
                self.assertFalse(ack['accepted'])
                now[0] += .05
                unknown = receiver.receive('viewer', packet(2, ack, video_age_ms=None))
                self.assertEqual(unknown['reason'], 'video-unknown')
                self.assertFalse(unknown['active'])
                now[0] += .05
                old = receiver.receive('viewer', packet(3, unknown, video_age_ms=290))
                self.assertEqual(old['reason'], 'video-age')
                # Pause is immediate even inside the normal receive rate limit.
                receiver.receive('viewer', packet(4, old, enabled=False, active=False))
                self.assertTrue(receiver.paused())
                receiver.open('viewer')
                with self.assertRaisesRegex(ValueError, 'session has changed'):
                    receiver.receive('viewer', packet(5, old))
            finally:
                receiver.close()

    def test_freshness_thresholds_are_configurable(self):
        from robot_runtime.av.settings_config import load_profile
        policy = load_profile(overrides={'input_max_age_ms': 100, 'input_timeout_ms': 40})
        with tempfile.TemporaryDirectory() as directory:
            now = [10.0]
            receiver = GamepadReceipt(Path(directory), clock=lambda: now[0], policy=policy)
            try:
                lease = receiver.open('viewer')
                now[0] += .02
                receiver.receive('viewer', packet(1, lease))
                now[0] += .041
                self.assertFalse(receiver.snapshot()['viewer']['fresh'])
                now[0] += .04
                self.assertFalse(receiver.receive('viewer', packet(2, lease))['accepted'])
            finally:
                receiver.close()


if __name__ == '__main__':
    unittest.main()
