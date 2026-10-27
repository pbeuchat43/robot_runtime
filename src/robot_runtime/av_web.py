"""Same-origin AV gateway with short, independently enforced worker leases."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import asyncio
import contextlib
import time

from aiohttp import ClientError, ClientSession, ClientTimeout, UnixConnector, WSMsgType, web

from .common import Fault, dumps, fields

IDENTITY = ("run_id", "config_revision", "generation", "source_run")
READS = {"config", "stats", "clock", "cameras"}
COMMANDS = {"control/sessions", "control/close", "media/sessions", "media/close", "media/renew",
            "settings", "cameras/refresh", "camera", "telemetry"}


class AVGateway:
    def __init__(self, adapter):
        self.adapter = adapter
        self.bindings = {}
        self.sockets = {}
        self.slots = asyncio.Semaphore(8)
        self.closing = False

    def context(self, target, owner=None):
        return dict(gateway=self.adapter.generation, owner=owner, run_id=target["run_id"],
                    config_revision=target["config_revision"], source_run=target["source_run"], source_mode=target.get("source_mode"))

    async def call(self, target, route, body=None, owner=None, **identity):
        try:
            async with ClientSession(connector=UnixConnector(path=target["socket"]),
                                     timeout=ClientTimeout(total=15)) as client:
                async with client.post("http://localhost/call", json=dict(route=route, body=body or {},
                        context=self.context(target, owner), **identity)) as response:
                    value = await response.json()
                    if response.status != 200:
                        if isinstance(value.get("error"), dict):
                            raise Fault(value["error"]["code"], value["error"]["message"])
                        raise Fault("av_unavailable", "The native service rejected this operation")
                    return value
        except (ClientError, OSError, asyncio.TimeoutError) as exc:
            raise Fault("av_unavailable", "Native service unavailable; operation outcome may be unknown") from exc

    async def catalog(self, request):
        if request.query:
            raise Fault("invalid_request", "AV catalog takes no query fields")
        return web.json_response(await self.adapter.rpc("av_catalog"))

    async def target(self, capability, *, fresh=False):
        if self.closing:
            raise Fault("av_unavailable", "Browser gateway is closing")
        return await self.adapter.rpc("av_target", capability_id=capability, fresh=fresh)

    async def handle(self, request):
        route = request.match_info["route"]
        if request.query or route not in (READS if request.method == "GET" else COMMANDS):
            raise Fault("not_found", "Unknown video/input operation")
        if self.slots.locked():
            raise Fault("busy", "Video/input request limit reached")
        async with self.slots:
            owner = value = None
            if request.method == "POST":
                owner = self.adapter.session(request, required=True, csrf=True)["id"]
                value = await self.adapter.body(request.clone(client_max_size=131072))
                fields(value, ("request_id", "expires_at_ms", "config_revision", "generation",
                               "expected_run", "source_run", "arguments"))
                if not isinstance(value["arguments"], dict):
                    raise Fault("invalid_request", "Arguments must be an object")
            target = await self.target(request.match_info["capability"], fresh=value is not None)
            if value is None:
                result = await self.call(target, route)
                if route == "config":
                    result["runtime"] = {key: target[key] for key in IDENTITY}
                    result["server_time_ms"] = int(time.time() * 1000)
                return web.json_response(result)
            for key in IDENTITY:
                field = "expected_run" if key == "run_id" else key
                if value[field] != target[key]:
                    raise Fault("stale_run", "Video/input ownership changed; refresh and connect explicitly")
            # Register before negotiation, which may outlast one lease interval.
            self.bindings[target["id"]] = target
            result = await self.call(target, route, value["arguments"], owner,
                                     request_id=value["request_id"], expires_at_ms=value["expires_at_ms"])
            session = self.adapter.session(request, required=True)
            if session["id"] != owner or self.bindings.get(target["id"]) != target:
                raise Fault("unauthorized", "Video/input authorization changed during this operation")
            return web.json_response(result)

    async def signal(self, request):
        if (request.headers.get("Origin") != self.adapter.origin or set(request.query) != {"token"}
                or len(request.query.getall("token", [])) != 1 or len(request.query["token"]) > 64):
            raise Fault("forbidden", "Signalling requires the robot origin and one subscription token")
        owner = self.adapter.session(request, required=True)["id"]
        if len(self.sockets) >= 40:
            raise Fault("busy", "Signalling connection limit reached")
        target = await self.target(request.match_info["capability"], fresh=True)
        self.bindings[target["id"]] = target
        downstream = web.WebSocketResponse(max_msg_size=65536, heartbeat=5, timeout=1)
        self.sockets[downstream] = (target, owner)
        try:
            async with ClientSession(connector=UnixConnector(path=target["socket"]),
                                     timeout=ClientTimeout(total=5)) as client:
                async with client.ws_connect("http://localhost/signal", params={"token": request.query["token"]},
                        headers={"X-Runtime-Context": dumps(self.context(target, owner))},
                        max_msg_size=65536, heartbeat=5) as upstream:
                    if (self.adapter.session(request, required=True)["id"] != owner
                            or self.bindings.get(target["id"]) != target):
                        raise Fault("unauthorized", "Signalling authorization changed during connection")
                    await downstream.prepare(request)

                    async def copy(source, destination):
                        async for message in source:
                            if message.type != WSMsgType.TEXT:
                                break
                            await asyncio.wait_for(destination.send_str(message.data), 2)

                    tasks = [asyncio.create_task(copy(downstream, upstream)), asyncio.create_task(copy(upstream, downstream))]
                    try:
                        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
        except (ClientError, OSError, asyncio.TimeoutError) as exc:
            if not downstream.prepared:
                raise Fault("av_unavailable", "Subscription expired or signalling unavailable") from exc
        finally:
            self.sockets.pop(downstream, None)
            if downstream.prepared:
                await downstream.close()
        return downstream

    async def revoke(self, target):
        if self.bindings.get(target["id"]) == target:
            self.bindings.pop(target["id"], None)
        # Worker expiry remains effective even when its socket is unreachable.
        with contextlib.suppress(Fault, asyncio.TimeoutError):
            await asyncio.wait_for(self.call(target, "invalidate"), .7)
        for socket, (bound, _owner) in tuple(self.sockets.items()):
            if bound == target and socket.prepared:
                await socket.close(code=1008, message=b"Runtime ownership changed")

    async def heartbeat(self):
        while not self.closing:
            try:
                self.adapter.auth.reload()
                self.adapter.auth.expire()
                owners = {s["id"] for s in self.adapter.auth.sessions.values()}
            except (Fault, OSError):
                owners = set()

            async def renew(target):
                try:
                    current = await self.target(target["id"])
                    if any(current[k] != target[k] for k in IDENTITY):
                        raise Fault("stale_run", "Runtime identity changed")
                    await asyncio.wait_for(self.call(target, "heartbeat", dict(owners=sorted(owners))), .7)
                except (Fault, asyncio.TimeoutError):
                    await self.revoke(target)

            await asyncio.gather(*(renew(target) for target in tuple(self.bindings.values())))
            for socket, (_target, owner) in tuple(self.sockets.items()):
                if owner not in owners and socket.prepared:
                    await socket.close(code=1008, message=b"Authentication expired")
            await asyncio.sleep(.75)

    async def close(self):
        self.closing = True
        await asyncio.gather(*(self.revoke(target) for target in tuple(self.bindings.values())))
