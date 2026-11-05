"""Stopped-state maintenance boundary for deployment owners; no lifecycle replay."""
# Copyright (c) 2026 Paul N. Beuchat. MIT License.
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from .common import Fault
from .store import Store

STATE_SCHEMA = 1


@contextmanager
def stopped(state, *, backup=None):
    """Hold the manager's lock and prove retained ownership is resolved.

The caller must stop its management service before entry. Existing manager
ownership, any blocked run or unfinished operation fails closed. Terminal runs
remain history; changing profiles does not restore or replay old processes.
"""
    store = Store(state)
    try:
        if any(run["blocked"] for run in store.runs()):
            raise Fault("workloads_owned", "Stop and resolve retained workloads through runtime before maintenance")
        if any(operation.get("phase") in ("prepared", "in_progress") or
               (operation.get("action") == "restart" and operation.get("phase") == "outcome_unknown")
               for operation in store.operations()):
            raise Fault("operations_unresolved", "Resolve retained operations before maintenance")
        if backup is not None:
            path = Path(backup)
            if path.exists():
                raise Fault("backup_exists", "Choose a new state backup destination")
            with path.open("xb"):
                pass
            path.chmod(0o600)
            connection = sqlite3.connect(path)
            try:
                store.db.backup(connection)
            finally:
                connection.close()
        yield dict(state_schema=STATE_SCHEMA, installation=store.meta("installation"),
                   retained_runs=len(store.runs()), automatic_replay=False)
    finally:
        store.close()
