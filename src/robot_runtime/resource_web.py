"""One manager resource stream shared by the browser's resource panels."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import asyncio
import contextlib
import time
from collections import deque
from pathlib import Path

from .common import Fault
from .ipc import MAX_RESPONSE, decode_response
from .resource_monitor import HISTORY_COLUMNS, RETENTION


class ResourceFanout:
    def __init__(self, state):
        self.state = state
        self.viewers = set()
        self.task = None
        self.lock = asyncio.Lock()
        self.closing = False
        self.latest = dict(fresh=False, latest=None, error="Waiting for resource monitoring")
        self.received = time.monotonic()
        self.history = deque(maxlen=RETENTION)
        self.stream_revision = 0

    def snapshot(self, history=False):
        elapsed = max(0, time.monotonic() - self.received)
        value = dict(self.latest, stream_revision=self.stream_revision)
        if value.get("age_seconds") is not None:
            value["age_seconds"] += elapsed
        value["fresh"] = bool(value.get("fresh") and value.get("age_seconds", 999) <= value["stale_after_seconds"])
        if self.latest.get("latest"):
            now = self.latest["latest"]["monotonic"] + (value.get("age_seconds") or 0)
            while self.history and now - self.history[0][1] >= RETENTION:
                self.history.popleft()
        if history:
            value.update(history=list(self.history), history_columns=HISTORY_COLUMNS)
        return value

    def publish(self, value):
        if "history" in value:
            self.history = deque(value["history"], maxlen=RETENTION)
            self.stream_revision += 1
        sample = value.get("latest")
        if sample and (not self.history or sample["sequence"] > self.history[-1][0]):
            host, gpu = sample.get("host", {}), sample.get("gpu", {})
            memory = 100 * host["memory_used_bytes"] / host["memory_total_bytes"] if host.get("memory_total_bytes") and host.get("memory_used_bytes") is not None else None
            self.history.append([sample["sequence"], sample["monotonic"], sample["time_ms"], host.get("cpu_percent"), memory, gpu.get("activity_percent")])
        self.latest = {key: item for key, item in value.items() if key not in ("history", "history_columns")}
        self.received = time.monotonic()
        for channel in self.viewers:
            if channel.full():
                channel.get_nowait()
            channel.put_nowait(True)

    async def add(self, channel=None):
        async with self.lock:
            if self.closing or len(self.viewers) >= 8:
                raise Fault("busy", "Resource observation connection limit reached")
            channel = channel or asyncio.Queue(maxsize=1)
            self.viewers.add(channel)
            if self.task is None:
                self.task = asyncio.create_task(self.upstream())
            if not channel.full():
                channel.put_nowait(True)
            return channel

    async def stop(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        self.latest = dict(fresh=False, latest=None, error="Waiting for resource monitoring")
        self.history.clear()

    async def remove(self, channel):
        async with self.lock:
            self.viewers.discard(channel)
            if not self.viewers:
                await self.stop()

    async def close(self):
        async with self.lock:
            self.closing = True
            for channel in self.viewers:
                if channel.full():
                    channel.get_nowait()
                channel.put_nowait(False)
            await self.stop()

    async def upstream(self):
        while True:
            writer = None
            try:
                reader, writer = await asyncio.open_unix_connection(str(Path(self.state) / "runtime.sock"), limit=MAX_RESPONSE + 1)
                writer.write(b'{"version":1,"action":"resource_watch"}\n')
                await writer.drain()
                while True:
                    value = decode_response(await asyncio.wait_for(reader.readline(), 15))
                    if value.get("type") == "snapshot":
                        self.publish(value["status"])
            except (OSError, Fault, ValueError, asyncio.TimeoutError):
                self.publish(dict(self.snapshot(), fresh=False, error="Resource manager connection unavailable"))
            finally:
                if writer:
                    writer.close()
                    with contextlib.suppress(OSError):
                        await writer.wait_closed()
            await asyncio.sleep(1)
