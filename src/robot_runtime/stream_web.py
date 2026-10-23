"""One bounded SSE connection per page, with independently identified updates."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import asyncio
import time
import uuid

from aiohttp import web

from .common import Fault, dumps
from .ipc import MAX_RESPONSE
from .ros_profile import MAX_TOPICS


async def stream_events(adapter, request):
    channels = request.query.getall("channel", [])
    selected = request.query.getall("topic", [])
    if (set(request.query) - {"channel", "topic"} or not channels or len(channels) > 3
            or len(set(channels)) != len(channels)
            or not set(channels) <= {"runtime", "observations", "resources"}
            or bool(selected) != ("observations" in channels)):
        raise Fault("invalid_request", "Choose channels and observation topic IDs")
    if adapter.closing or len(adapter.stream_viewers) >= 8:
        raise Fault("busy", "Page observation connection limit reached")
    interval = 0
    if selected:
        catalog = await adapter.rpc("ros_status")
        known = {topic["id"]: topic for topic in catalog["topics"]}
        if not 1 <= len(selected) <= MAX_TOPICS or not set(selected) <= known.keys():
            raise Fault("not_found", "Unknown ROS topic capability")
        interval = 1 / max(known[key]["max_rate_hz"] for key in selected)
    # Reserve after the awaited catalog read as other admissions may have run.
    if adapter.closing or len(adapter.stream_viewers) >= 8:
        raise Fault("busy", "Page observation connection limit reached")
    wake = asyncio.Queue(maxsize=1)
    adapter.stream_viewers.add(wake)
    registered = set()
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                           "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    try:
        if "runtime" in channels:
            if len(adapter.viewers) >= 8:
                raise Fault("busy", "Runtime observation connection limit reached")
            adapter.viewers.add(wake)
            registered.add("runtime")
        if selected:
            await adapter.ros.add(selected, wake)
            registered.add("observations")
        if "resources" in channels:
            await adapter.resources.add(wake)
            registered.add("resources")
        if not wake.full():
            wake.put_nowait(True)
        await response.prepare(request)
        stream_id, sequence = uuid.uuid4().hex, 0
        cursor, seen = {}, {}
        next_write = 0
        while not adapter.closing:
            try:
                await asyncio.wait_for(wake.get(), 5)
                await asyncio.sleep(max(0, next_write - time.monotonic()))
                while not wake.empty():
                    wake.get_nowait()
                if adapter.closing:
                    break
                parts = []
                if "runtime" in registered and seen.get("runtime") != adapter.received:
                    parts.append('"runtime":' + dumps(adapter.snapshot()))
                    seen["runtime"] = adapter.received
                if selected and seen.get("observations") != (adapter.ros.epoch, adapter.ros.received):
                    parts.append('"observations":' + adapter.ros.updates(selected, cursor))
                    seen["observations"] = (adapter.ros.epoch, adapter.ros.received)
                if "resources" in registered and seen.get("resources") != adapter.resources.received:
                    parts.append('"resources":' + dumps(adapter.resources.snapshot(
                        history=seen.get("resource_revision") != adapter.resources.stream_revision)))
                    seen["resources"] = adapter.resources.received
                    seen["resource_revision"] = adapter.resources.stream_revision
                if not parts:
                    continue
                sequence += 1
                header = dumps(dict(version=2, stream_id=stream_id, sequence=sequence))
                data = ("data: " + header[:-1] + "," + ",".join(parts) + "}\n\n").encode()
                if len(data) > MAX_RESPONSE:
                    break
            except asyncio.TimeoutError:
                data = b": heartbeat\n\n"
            next_write = time.monotonic() + interval
            await asyncio.wait_for(response.write(data), 2)
    except (ConnectionError, asyncio.TimeoutError):
        pass
    finally:
        adapter.stream_viewers.discard(wake)
        adapter.viewers.discard(wake)
        if "observations" in registered:
            await adapter.ros.remove(wake)
        if "resources" in registered:
            await adapter.resources.remove(wake)
    return response
