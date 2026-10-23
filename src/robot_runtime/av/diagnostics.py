# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
"""Optional bounded file logging; storage never runs on the input callback."""
import json
from queue import Empty, Full, Queue
import threading


class JsonLog:
    def __init__(self, path, enabled=True, capacity=128):
        self.enabled = enabled
        self.dropped = 0
        self.error = None
        self.queue = Queue(maxsize=capacity)
        self.stopped = threading.Event()
        self.thread = None
        if enabled:
            self.file = path.open('w', buffering=1)
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def write(self, value):
        if not self.enabled or self.stopped.is_set():
            return
        try:
            self.queue.put_nowait(value)
        except Full:
            self.dropped += 1

    def _run(self):
        try:
            while not self.stopped.is_set() or not self.queue.empty():
                try:
                    value = self.queue.get(timeout=.1)
                except Empty:
                    continue
                self.file.write(json.dumps(value) + '\n')
        except (OSError, ValueError, TypeError) as error:
            self.error = str(error)
        finally:
            self.file.close()

    def close(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=1)
