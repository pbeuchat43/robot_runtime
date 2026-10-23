"""One private manager ROS watch shared by bounded browser consumers."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import asyncio
import contextlib
import time
from pathlib import Path

from .common import Fault, dumps
from .ipc import MAX_RESPONSE, decode_response
from .observation_delivery import split_topic


class TopicFanout:
    def __init__(self, state):
        self.state = state
        self.viewers = {}
        self.topics = set()
        self.task = None
        self.lock = asyncio.Lock()
        self.latest = None
        self.values, self.metadata, self.records = {}, {}, {}
        self.serial = 0
        self.epoch = 0
        self.received = time.monotonic()
        self.closing = False

    def snapshot(self, selected):
        if self.latest is None:
            return dict(available=False, topics=[], error="Waiting for ROS observations")
        elapsed = max(0, time.monotonic() - self.received)
        result = dict(self.latest, server_time_ms=int(time.time() * 1000))
        values = []
        for topic, at in self.values.values():
            if topic["id"] not in selected:
                continue
            value = dict(topic)
            elapsed = max(0, time.monotonic() - at)
            for key in ("age_seconds", "acquisition_age_seconds", "sample_age_seconds", "progress_age_seconds"):
                if value.get(key) is not None:
                    value[key] += elapsed
            value["fresh"] = bool(value.get("fresh") and value.get("age_seconds", 999) <= value["stale_after_seconds"]
                                  and (value.get("acquisition_age_seconds") is None or value["acquisition_age_seconds"] <= value["stale_after_seconds"])
                                  and (value.get("sample_age_seconds") is None or value["sample_age_seconds"] < value["stale_after_seconds"]))
            if not value["fresh"] and value["status"] == "current":
                value["status"] = "stale"
            values.append(value)
        result["topics"] = values
        return result

    def publish(self, value):
        # Complete legacy snapshots can still be injected by fixtures.
        self.apply(dict(value, reset=True))

    def apply(self, value):
        now = time.monotonic()
        if value.get("reset"):
            self.values.clear()
            self.metadata.clear()
            self.records.clear()
            self.epoch += 1
        self.latest = {key: item for key, item in value.items()
                       if key not in ("topics", "metadata", "reset", "sampled_monotonic")}
        self.received = now
        for spec in value.get("metadata", []):
            self.metadata[spec["id"]] = spec
        at = value.get("sampled_monotonic", now)
        for sample in value["topics"]:
            key = sample["id"]
            # The private update stream has separate metadata. The fallback
            # accepts complete v1 topic values for tests/legacy publication.
            if "stale_after_seconds" in sample:
                self.metadata[key], sample = split_topic(sample)
            topic = dict(self.metadata[key], **sample)
            self.values[key] = (topic, at)
            self.serial += 1
            self.records[key] = (self.serial, dumps(dict(at=at, value=sample)))
        for channel in tuple(self.viewers):
            if channel.full():
                channel.get_nowait()
            channel.put_nowait(True)

    def updates(self, selected, cursor):
        """Encode each sample once, reuse across viewers; never queue frames."""
        reset = cursor.get("epoch") != self.epoch
        if reset:
            cursor.clear()
            cursor["epoch"] = self.epoch
        metadata, records = [], []
        for key in sorted(selected):
            if key not in self.records:
                continue
            revision, encoded = self.records[key]
            spec = self.metadata[key]
            if cursor.get(("metadata", key)) != spec:
                metadata.append(spec)
                cursor[("metadata", key)] = spec
            if cursor.get(key) != revision:
                records.append(encoded)
                cursor[key] = revision
        header = self.latest or dict(available=False, error="Waiting for ROS observations")
        envelope = dict(reset=reset, header=header, metadata=metadata, sent_at=time.monotonic())
        return dumps(envelope)[:-1] + ',"updates":[' + ",".join(records) + "]}"

    async def sync(self):
        selected = set().union(*self.viewers.values()) if self.viewers and not self.closing else set()
        if selected == self.topics:
            return
        self.topics = selected
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        if selected:
            self.task = asyncio.create_task(self.upstream(sorted(selected)))
        else:
            self.latest = None
            self.values.clear()
            self.metadata.clear()
            self.records.clear()
            self.epoch += 1

    async def add(self, selected, channel=None):
        async with self.lock:
            if self.closing or len(self.viewers) >= 8:
                raise Fault("busy", "ROS observation connection limit reached")
            channel = channel or asyncio.Queue(maxsize=1)
            self.viewers[channel] = set(selected)
            await self.sync()
            if not channel.full():
                channel.put_nowait(True)
            return channel

    async def remove(self, channel):
        async with self.lock:
            self.viewers.pop(channel, None)
            await self.sync()

    async def close(self):
        async with self.lock:
            self.closing = True
            for channel in tuple(self.viewers):
                if channel.full():
                    channel.get_nowait()
                channel.put_nowait(False)
            await self.sync()

    async def upstream(self, selected):
        while True:
            writer = None
            try:
                reader, writer = await asyncio.open_unix_connection(str(Path(self.state) / "runtime.sock"), limit=MAX_RESPONSE + 1)
                writer.write((dumps(dict(version=1, action="ros_watch", topics=selected, updates=True)) + "\n").encode())
                await writer.drain()
                while True:
                    value = decode_response(await asyncio.wait_for(reader.readline(), 15))
                    if value.get("type") == "snapshot":
                        self.apply(value["status"])
            except (OSError, Fault, ValueError, asyncio.TimeoutError):
                value = self.snapshot(selected)
                value.update(available=False, error="ROS manager connection unavailable")
                value["topics"] = [dict(topic, fresh=False, status="worker_unavailable") for topic in value["topics"]]
                self.publish(value)
            finally:
                if writer:
                    writer.close()
                    with contextlib.suppress(OSError):
                        await writer.wait_closed()
            await asyncio.sleep(1)
