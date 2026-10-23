"""Bounded log pages and explicit cursor gaps."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import json
import unittest
from unittest.mock import patch

from robot_runtime.common import dumps
from robot_runtime.journal import read


class JournalTests(unittest.TestCase):
    def test_large_messages_stay_within_response_budget(self):
        raw = b"".join((json.dumps(dict(__CURSOR="cursor" + str(i), __REALTIME_TIMESTAMP="1234", MESSAGE="x" * 6000)) + "\n").encode() for i in range(100))
        with patch("robot_runtime.journal.capture", return_value=(raw, False, b"")):
            page = read(dict(invocation="abc", boot_id="def"))
        self.assertLess(len(dumps(page).encode()), 66000)
        self.assertTrue(page["truncated"])
        self.assertTrue(all(record["message_truncated"] for record in page["records"]))

    def test_missing_cursor_is_reported_as_gap(self):
        check = (b'{"__CURSOR":"different"}\n', False, b"")
        with patch("robot_runtime.journal.capture", side_effect=[check, (b"", False, b"")]):
            page = read(dict(invocation="abc", boot_id="def"), cursor="old")
        self.assertEqual(page["gap"], "cursor_not_retained_or_not_in_this_run")
        self.assertFalse(page["history_complete"])


if __name__ == "__main__":
    unittest.main()
