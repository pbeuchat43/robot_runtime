"""Single-manager durable intent. Observation counters stay out of the database."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import fcntl
import os
import sqlite3
import uuid
from pathlib import Path

from .common import Fault, dumps, loads


class Store:
    def __init__(self, directory):
        supplied = Path(directory)
        if supplied.is_symlink():
            raise Fault("unsafe_state", "State directory cannot be a symlink")
        supplied.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory = supplied.resolve()
        stat = self.directory.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise Fault("unsafe_state", "State directory must be owned by this account and mode 0700")
        self.lock = (self.directory / "manager.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            raise Fault("manager_running", "Another manager owns this state directory") from exc
        os.chmod(self.directory / "manager.lock", 0o600)
        self.db = sqlite3.connect(self.directory / "runtime.sqlite3")
        os.chmod(self.directory / "runtime.sqlite3", 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.close()
            raise Fault("state_version", "Unsupported state schema; no automatic migration")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, unit TEXT NOT NULL, blocked INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_live_run ON runs(unit) WHERE blocked=1;
            CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            PRAGMA user_version=1;
        """)
        if not self.meta("installation"):
            self.set_meta("installation", uuid.uuid4().hex)

    def close(self):
        self.db.close()
        self.lock.close()

    def meta(self, key):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, value))

    def runs(self):
        return [loads(row[0]) for row in self.db.execute("SELECT data FROM runs ORDER BY rowid")]

    def operation(self, request_id):
        row = self.db.execute("SELECT data FROM operations WHERE id=?", (request_id,)).fetchone()
        return loads(row[0]) if row else None

    def operations(self):
        return [loads(row[0]) for row in self.db.execute("SELECT data FROM operations")]

    def capacity(self):
        if self.db.execute("SELECT count(*) FROM operations").fetchone()[0] >= 5000:
            raise Fault("history_full", "Operation history limit reached; archive state during stopped maintenance")

    def save(self, *, run=None, operation=None):
        with self.db:
            if run is not None:
                self.db.execute("INSERT INTO runs VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET unit=excluded.unit, blocked=excluded.blocked, data=excluded.data",
                                (run["run_id"], run["unit_id"], int(run["blocked"]), dumps(run)))
            if operation is not None:
                self.db.execute("INSERT OR REPLACE INTO operations VALUES (?,?)",
                                (operation["request_id"], dumps(operation)))
