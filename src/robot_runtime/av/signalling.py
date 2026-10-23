"""Single-producer signalling with admission before any native media session.

Implements the pinned gstwebrtc 0.14.5 wire protocol. The producer uses a
loopback-only bearer-authenticated socket; consumers use private Unix IPC.
"""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import asyncio
import contextlib
import hmac
import secrets
import time

from aiohttp import WSMsgType, web

from ..common import Fault, fields, loads, text


MAX_SIGNAL = 65536


async def send(peer, value):
    if peer is not None and not peer.closed:
        await asyncio.wait_for(peer.send_json(value), 2)


def peer_payload(value, *, producer):
    fields(value, ("type", "sessionId"), ("sdp", "ice"))
    if ("sdp" in value) == ("ice" in value):
        raise Fault("invalid_request", "Expected one SDP or ICE payload")
    if "sdp" in value:
        fields(value["sdp"], ("type", "sdp"))
        if value["sdp"]["type"] != ("offer" if producer else "answer"):
            raise Fault("forbidden", "Unexpected media SDP role")
        sdp = text(value["sdp"]["sdp"], 60000)
        if "m=video " not in sdp or "m=audio " in sdp or "m=application " in sdp:
            raise Fault("forbidden", "This session supports receive-only video")
        # The consumer must not use the media connection to publish back.
        if not producer and any(line in ("a=sendrecv", "a=sendonly") for line in sdp.splitlines()):
            raise Fault("forbidden", "Consumer SDP must be receive-only")
    else:
        fields(value["ice"], ("candidate", "sdpMLineIndex"), ("sdpMid", "usernameFragment"))
        text(value["ice"]["candidate"], 4096, empty=True)
        if type(value["ice"]["sdpMLineIndex"]) is not int or not 0 <= value["ice"]["sdpMLineIndex"] <= 4:
            raise Fault("invalid_request", "Invalid ICE media index")
    return value


class Signalling:
    def __init__(self, sessions):
        self.sessions = sessions
        self.secret = secrets.token_urlsafe(32)
        self.producer = None
        self.producer_id = None
        self.producer_ready = False
        self.consumers = {}
        self.media = {}
        self.runner = None

    async def start(self):
        app = web.Application(client_max_size=MAX_SIGNAL)
        app.router.add_get("/producer", self.producer_connection)
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        return "ws://127.0.0.1:" + str(self.runner.addresses[0][1]) + "/producer"

    async def producer_connection(self, request):
        if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + self.secret):
            raise web.HTTPForbidden()
        if self.producer is not None and not self.producer.closed:
            raise web.HTTPConflict()
        peer = web.WebSocketResponse(max_msg_size=MAX_SIGNAL, heartbeat=10, timeout=1)
        await peer.prepare(request)
        self.producer, self.producer_id = peer, secrets.token_hex(16)
        self.producer_ready = False
        await send(peer, dict(type="welcome", peerId=self.producer_id))
        try:
            async for message in peer:
                if message.type != WSMsgType.TEXT:
                    break
                value = loads(message.data)
                if not isinstance(value, dict):
                    raise Fault("invalid_request", "Expected a signalling object")
                kind = value.get("type")
                if kind == "setPeerStatus":
                    fields(value, ("type", "roles"), ("meta", "peerId"))
                    if value["roles"] != ["producer"]:
                        raise Fault("forbidden", "Native media must register as producer")
                    self.producer_ready = True
                    status = dict(type="peerStatusChanged", peerId=self.producer_id, roles=["producer"], meta={"name": "Runtime video"})
                    await send(peer, status)
                    for consumer in tuple(self.consumers.values()):
                        await send(consumer["peer"], status)
                elif kind == "peer":
                    target = self.media.get(value.get("sessionId"))
                    if target and self.sessions.allowed(target):
                        await send(self.consumers[target]["peer"], peer_payload(value, producer=True))
                elif kind == "endSession":
                    fields(value, ("type", "sessionId"))
                    await self.end(value["sessionId"])
                else:
                    raise Fault("invalid_request", "Unsupported native signalling message")
        except (Fault, OSError, asyncio.TimeoutError):
            await peer.close(code=1008)
        finally:
            if self.producer is peer:
                self.producer_ready = False
                self.producer = None
                for token in tuple(self.consumers):
                    await self.close_consumer(token)
        return peer

    async def consumer_connection(self, request, token, context):
        entry = self.sessions.require(token, context)
        if entry["kind"] != "media" or token in self.consumers:
            raise Fault("busy", "This subscription already has a signalling connection")
        peer = web.WebSocketResponse(max_msg_size=MAX_SIGNAL, heartbeat=10, timeout=1)
        # Reserve before preparing: a concurrent handshake cannot duplicate a grant.
        current = dict(peer=peer, id=secrets.token_hex(16), session=None, last_start=-10, window=time.monotonic(), count=0)
        self.consumers[token] = current
        try:
            await peer.prepare(request)
            await send(peer, dict(type="welcome", peerId=current["id"]))
            async for message in peer:
                self.sessions.require(token, context)
                if message.type != WSMsgType.TEXT:
                    break
                now = time.monotonic()
                if now - current["window"] >= 1:
                    current.update(window=now, count=0)
                current["count"] += 1
                if current["count"] > 100:
                    raise Fault("rate_limited", "Signalling rate exceeded")
                value = loads(message.data)
                if not isinstance(value, dict):
                    raise Fault("invalid_request", "Expected a signalling object")
                kind = value.get("type")
                if kind == "setPeerStatus":
                    fields(value, ("type", "roles"), ("meta",))
                    if value["roles"] != ["listener"]:
                        raise Fault("forbidden", "Video viewers cannot register as producers")
                    await send(peer, dict(type="peerStatusChanged", peerId=current["id"], roles=["listener"], meta={}))
                elif kind in ("list", "listConsumers"):
                    fields(value, ("type",))
                    if kind == "list":
                        await send(peer, dict(type="list", producers=[dict(id=self.producer_id, meta={"name": "Runtime video"})] if self.producer_ready else []))
                    else:
                        await send(peer, dict(type="listConsumers", consumers=[]))
                elif kind == "startSession":
                    fields(value, ("type", "peerId"))
                    if value["peerId"] != self.producer_id or not self.producer_ready:
                        raise Fault("av_unavailable", "Video producer is unavailable")
                    if current["session"] or now - current["last_start"] < 1:
                        raise Fault("busy", "One admitted media session per subscription")
                    session = secrets.token_hex(16)
                    current.update(session=session, last_start=now)
                    self.media[session] = token
                    await send(peer, dict(type="sessionStarted", peerId=self.producer_id, sessionId=session))
                    await send(self.producer, dict(type="startSession", peerId=current["id"], sessionId=session, offer=None))
                elif kind in ("peer", "endSession"):
                    if not current["session"] or value.get("sessionId") != current["session"]:
                        raise Fault("forbidden", "Media session belongs to another subscriber")
                    if kind == "peer":
                        await send(self.producer, peer_payload(value, producer=False))
                    else:
                        fields(value, ("type", "sessionId"))
                        await self.end(value["sessionId"])
                else:
                    raise Fault("invalid_request", "Unsupported viewer signalling message")
        except (Fault, OSError, asyncio.TimeoutError):
            await peer.close(code=1008)
        finally:
            if current["session"]:
                await self.end(current["session"])
            self.consumers.pop(token, None)
        return peer

    async def end(self, session):
        token = self.media.pop(session, None)
        if token is None:
            return
        current = self.consumers.get(token)
        if current:
            current["session"] = None
        value = dict(type="endSession", sessionId=session)
        for peer in (self.producer, current["peer"] if current else None):
            with contextlib.suppress(OSError, asyncio.TimeoutError):
                await send(peer, value)

    async def close_consumer(self, token):
        current = self.consumers.get(token)
        if current:
            if current["session"]:
                await self.end(current["session"])
            await current["peer"].close(code=1001)

    async def close(self):
        for token in tuple(self.consumers):
            await self.close_consumer(token)
        if self.producer is not None:
            await self.producer.close(code=1001)
        if self.runner:
            await self.runner.cleanup()
