"""Stopped maintenance never borrows a live manager or unresolved workload."""
# Copyright (c) 2026 Paul N. Beuchat. MIT License.
from pathlib import Path
import tempfile
import unittest

from robot_runtime.common import Fault
from robot_runtime.maintenance import stopped
from robot_runtime.store import Store


class MaintenanceTests(unittest.TestCase):
    def test_empty_state_backup_and_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with stopped(root / "state", backup=root / "backup.sqlite3") as evidence:
                self.assertEqual(evidence["state_schema"], 1)
                self.assertTrue((root / "backup.sqlite3").is_file())
                with self.assertRaises(Fault):
                    Store(root / "state")

    def test_blocked_run_and_unknown_operation_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            store = Store(state)
            store.save(run=dict(run_id="run", unit_id="worker", blocked=True))
            store.close()
            with self.assertRaisesRegex(Fault, "Stop and resolve"):
                with stopped(state):
                    self.fail("Must not enter maintenance")
            store = Store(state)
            store.save(run=dict(run_id="run", unit_id="worker", blocked=False))
            store.save(operation=dict(request_id="operation", action="restart", phase="outcome_unknown"))
            store.close()
            with self.assertRaisesRegex(Fault, "Resolve retained"):
                with stopped(state):
                    self.fail("Must not enter maintenance")
