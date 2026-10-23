"""Explicitly managed native video OR diagnostic input; private Unix IPC only."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import argparse
import asyncio
import contextlib
import os
from pathlib import Path
import re
import signal
import stat
import threading
import time

from aiohttp import web

from ..av_profile import socket_path
from ..common import Fault, fields, loads, text
from ..core import request_id
from ..profile import read
from .sessions import MEDIA_TTL, Sessions
from .settings_config import load_profile
from .signalling import Signalling

PUBLIC = {"config", "stats", "clock", "cameras"}


def declared_camera(source):
    """Describe only the configured source; never enumerate unrelated devices."""
    kind = source["kind"]
    value = dict(id=kind, kind=kind, label=source["label"], available=True)
    if kind == "oak":
        value.update(id="oak:" + source["serial"], serial=source["serial"])
    elif kind == "v4l2":
        from .cameras import video_formats
        from .native_modes import enumerate_modes
        path = source["path"]
        formats = video_formats(path)
        modes = enumerate_modes(path, formats)
        value.update(id="v4l2:" + path, kind="webcam", path=path, formats=sorted(formats),
                     modes=modes, native_mode_count=len(modes))
    elif kind == "ros":
        value["topic"] = source["topic"]
    return [value], []


class Worker:
    def __init__(self, config, revision, service, run, state):
        self.config, self.revision, self.service, self.run, self.state = config, revision, service, run, state
        self.sessions = Sessions(service["max_sessions"])
        self.control = self.stream = self.signalling = None
        self.mutation = asyncio.Lock()
        self.releasing = set()
        self.stopping = False
        self.profiles = {name: load_profile(name, overrides=service.get("profiles", {}).get(name, {})) for name in ("observer", "teleop")}
        self.output = state / ("av-" + service["id"] + "-" + run)
        self.output.mkdir(mode=0o700, exist_ok=False)
        policy = self.profiles["teleop" if service["kind"] == "input" else "observer"]
        self.args = argparse.Namespace(**policy, host=service["host"],
            control_port=service["udp_port"], rtc_port=service["udp_port"],
            max_control_sessions=service["max_sessions"], max_media_sessions=service["max_sessions"],
            control_negotiate_timeout_s=10, control_idle_timeout_s=3,
            media_connect_timeout_s=30, recovery=True, save_source_preview=False,
            profile="observer", encoder=service.get("encoder"), turn_port=None)

    async def start(self):
        if self.service["kind"] == "input":
            from .control_service import ControlService
            self.control = ControlService(self.args, self.output, self.sessions.allowed)
            return
        from functools import partial
        from .cameras import Cameras
        from .gst_runtime import Gst
        from .media_service import MediaStream
        source = self.service["source"]
        selector = {"oak": "oak:" + source.get("serial", ""), "v4l2": "v4l2:" + source.get("path", "")}.get(source["kind"], source["kind"])
        self.args.source = selector
        if source["kind"] == "ros":
            self.args.ros_topic, self.args.ros_domain_id = source["topic"], source["domain_id"]
        chosen = "nvv4l2h264enc" if self.service["encoder"] == "hardware" else "x264enc"
        self.args.encoder = chosen
        factory = Gst.ElementFactory.find(chosen)
        if factory is None:
            raise RuntimeError("Missing explicitly selected video encoder: " + chosen)
        factory.set_rank(Gst.Rank.PRIMARY + 100)
        self.signalling = Signalling(self.sessions)
        uri = await self.signalling.start()
        cameras = Cameras(selector, self.args.width, self.args.height, self.args.fps,
                          discover_fn=partial(declared_camera, source), policy=vars(self.args), active=False)
        self.stream = MediaStream(self.args, self.output, cameras, uri, self.signalling.secret)
        await asyncio.to_thread(self.stream.start)

    def context(self, value, *, owner=False):
        fields(value, ("gateway", "owner", "run_id", "config_revision", "source_run"))
        text(value["gateway"], 64)
        if owner:
            text(value["owner"], 64)
        elif value["owner"] is not None:
            text(value["owner"], 64)
        if value["run_id"] != self.run or value["config_revision"] != self.revision:
            raise Fault("stale_run", "Private service identity changed")
        if value["source_run"] is not None and not re.fullmatch(r"[0-9a-f]{32}", str(value["source_run"])):
            raise Fault("invalid_request", "Invalid source run")
        if self.service.get("source", {}).get("kind") == "ros" and value["source_run"] is None:
            raise Fault("av_unavailable", "A current autonomy source run is required")
        return value

    def configuration(self):
        policy = vars(self.stream.args) if self.stream else vars(self.args)
        # Explicit allowlist: native signalling credentials never enter public data.
        result = {key: value for key, value in policy.items() if key in load_profile()}
        result.update(run_id=self.run, capability_id=self.service["id"], kind=self.service["kind"],
            config_revision=self.revision, ice_servers=[], turn_port=None,
            control_negotiate_timeout_s=self.args.control_negotiate_timeout_s,
            control_policy=self.profiles["teleop"], media_admission=self.sessions.summary(),
            components=dict(version=1, control_transport="webrtc-datachannel", control_modes=["input", "fpv"], actuators_enabled=False))
        if self.stream:
            result.update(settings=self.stream.settings.snapshot(), profile=self.stream.args.profile,
                          encoder_factory="nvv4l2h264enc" if self.service["encoder"] == "hardware" else "x264enc")
        return result

    def snapshot(self):
        if self.stream:
            value = self.stream.snapshot()
            value["media_admission"] = dict(self.sessions.summary(), profile=self.stream.args.profile, max_active_profiles=1)
        else:
            # Public diagnostics carry no session capabilities or browser identities.
            value = dict(mono_ms=time.monotonic() * 1000, control_sessions=list(self.control.snapshot().values()),
                         gamepad=list(self.control.receiver.snapshot().values()), gamepad_ack_skipped=self.control.ack_skipped)
        return dict(value, run_id=self.run, actuators_enabled=False, admission=self.sessions.summary())

    async def release(self, token):
        entry = self.sessions.entries.get(token)
        if entry and token not in self.releasing:
            self.releasing.add(token)
            entry["authorized_until"] = 0
            try:
                if self.control:
                    await asyncio.to_thread(self.control.remove, token, "runtime session released")
                if self.signalling:
                    await self.signalling.close_consumer(token)
                self.sessions.remove(token)
            finally:
                self.releasing.discard(token)

    async def call(self, request):
        value = loads(await request.read())
        fields(value, ("route", "context", "body"), ("request_id", "expires_at_ms"))
        route = value["route"]
        if not isinstance(route, str):
            raise Fault("invalid_request", "Expected an AV operation")
        context = self.context(value["context"], owner=route not in PUBLIC | {"heartbeat", "invalidate"})
        body = value["body"]
        if not isinstance(body, dict):
            raise Fault("invalid_request", "Expected an object")
        if self.stopping:
            raise Fault("av_unavailable", "Service is stopping")
        if route in PUBLIC:
            fields(body, ())
            if route == "config":
                result = self.configuration()
            elif route == "clock":
                result = dict(mono_ms=time.monotonic() * 1000)
            elif route == "cameras" and self.stream:
                result = self.stream.cameras.snapshot(include_modes=True)
            elif route == "stats":
                result = await asyncio.to_thread(self.snapshot)
            else:
                raise Fault("not_found", "This input service has no cameras")
        elif route == "heartbeat":
            fields(body, ("owners",))
            if not isinstance(body["owners"], list) or len(body["owners"]) > 32:
                raise Fault("invalid_request", "Invalid authorized owners")
            owners = {text(owner, 64) for owner in body["owners"]}
            self.sessions.heartbeat(context["gateway"], owners, context["source_run"])
            result = dict(ok=True)
        elif route == "invalidate":
            fields(body, ())
            self.sessions.invalidate(context["gateway"])
            result = dict(ok=True)
        else:
            request_id(value.get("request_id"))
            expiry = value.get("expires_at_ms")
            if type(expiry) is not int or not time.time() * 1000 < expiry <= time.time() * 1000 + 60000:
                raise Fault("expired", "AV operation deadline expired or exceeds 60 seconds")
            result = await self.operation(route, body, context, value["request_id"])
        return web.json_response(result)

    async def operation(self, route, body, context, identity):
        if route == "control/sessions" and self.control:
            fields(body, ("mode", "sdp"))
            token, entry, created = self.sessions.acquire("input", context, identity, body)
            if not created:
                if entry["response"] is None:
                    raise Fault("busy", "Input negotiation is already in progress")
                return entry["response"]
            try:
                answer = await asyncio.to_thread(self.control.offer, body, token)
                self.sessions.require(token, context)
                entry["response"] = answer
                return answer
            except BaseException:
                await self.release(token)
                raise
        if route in ("control/close", "media/close", "media/renew"):
            field = "session_id" if route == "control/close" else "token"
            fields(body, (field,))
            token = text(body[field], 64)
            self.sessions.require(token, context)
            if route == "media/renew":
                self.sessions.renew(token, context)
            else:
                await self.release(token)
            return dict(ok=True)
        if not self.stream:
            raise Fault("not_found", "Unknown diagnostic input operation")
        if route == "media/sessions":
            fields(body, ("profile",))
            name = body["profile"]
            if name not in ("observer", "teleop", "current"):
                raise Fault("invalid_request", "Choose observer, teleop or current")
            if self.mutation.locked():
                raise Fault("busy", "Media configuration is changing")
            async with self.mutation:
                target = self.stream.args.profile if name == "current" else name
                if self.stream.settings.snapshot()["applying"]:
                    raise Fault("busy", "Media configuration is changing")
                if target != self.stream.args.profile:
                    if self.sessions.entries or self.stream.consumers:
                        raise Fault("busy", "A different media profile is in use; join current or disconnect viewers")
                    await asyncio.to_thread(self.stream.set_profile, target, self.profiles[target])
                token, entry, _ = self.sessions.acquire("media", context, identity, dict(body, profile=target))
                entry["response"] = dict(token=token, profile=target, ttl_s=MEDIA_TTL)
                return entry["response"]
        if route == "settings":
            if self.sessions.entries or self.mutation.locked():
                raise Fault("busy", "Disconnect video subscribers before changing shared settings")
            async with self.mutation:
                queued = self.stream.request_settings(body)
                return dict(ok=True, queued=queued, settings=self.stream.settings.snapshot())
        if route == "cameras/refresh":
            fields(body, ())
            return dict(ok=True, queued=self.stream.cameras.request_refresh())
        if route == "camera":
            fields(body, ("id",))
            # Only the exact declared source exists in this service's inventory.
            if body["id"] != self.stream.cameras.selected:
                raise Fault("forbidden", "Choose another declared media service to change source ownership")
            return dict(ok=True)
        if route == "telemetry":
            # Browser diagnostics are not runtime authority and are not retained.
            return dict(ok=True)
        raise Fault("not_found", "Unknown media operation")

    async def signal(self, request):
        if not self.signalling:
            raise Fault("not_found", "Input uses its own data-channel negotiation")
        context = self.context(loads(request.headers.get("X-Runtime-Context", "null")), owner=True)
        if set(request.query) != {"token"}:
            raise Fault("invalid_request", "Provide one admitted subscription token")
        return await self.signalling.consumer_connection(request, text(request.query["token"], 64), context)

    async def monitor(self):
        while not self.stopping:
            await asyncio.gather(*(self.release(token) for token in self.sessions.expired()))
            if self.control:
                await asyncio.to_thread(self.control.check)
                for token in tuple(self.sessions.entries):
                    if token not in self.control.peers and self.sessions.entries[token]["response"] is not None:
                        await self.release(token)
            if self.stream:
                await asyncio.to_thread(self.stream.check)
            await asyncio.sleep(.1)

    async def close(self):
        self.stopping = True
        for token in tuple(self.sessions.entries):
            await self.release(token)
        if self.control:
            await asyncio.to_thread(self.control.close)
        if self.stream:
            await asyncio.to_thread(self.stream.close)
        if self.signalling:
            await self.signalling.close()


async def serve(profile_path, state, capability):
    os.umask(0o077)
    config, revision = read(profile_path)
    run = os.environ.get("ROBOT_RUNTIME_RUN", "")
    if not re.fullmatch(r"[0-9a-f]{32}", run):
        raise Fault("forbidden", "Start this service through its declared runtime unit")
    service = next((s for s in config.get("av", {}).get("services", []) if s["id"] == capability), None)
    if service is None:
        raise Fault("not_found", "Unknown video/input capability")
    state = state.resolve()
    status = state.stat()
    if status.st_uid != os.getuid() or status.st_mode & 0o077 or not stat.S_ISDIR(status.st_mode):
        raise Fault("unsafe_state", "AV requires the private runtime state directory")
    endpoint = socket_path(state, capability, run)
    if len(os.fsencode(endpoint)) >= 108 or endpoint.exists():
        raise Fault("unsafe_state", "Private AV socket is too long or already exists")
    from .gst_runtime import GLib
    glib = GLib.MainLoop()
    native_thread = threading.Thread(target=glib.run)
    native_thread.start()
    worker = Worker(config, revision, service, run, state)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    monitor = runner = None

    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except Fault as exc:
            return web.json_response(dict(error=exc.as_dict()), status=400)
        except (ValueError, TypeError) as exc:
            return web.json_response(dict(error=dict(code="invalid_request", message=str(exc)[:500])), status=400)
        except (RuntimeError, TimeoutError) as exc:
            return web.json_response(dict(error=dict(code="av_unavailable", message=str(exc)[:500])), status=503)

    try:
        await worker.start()
        app = web.Application(middlewares=[errors], client_max_size=131072)
        app.add_routes([web.post("/call", worker.call), web.get("/signal", worker.signal)])
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
        await runner.setup()
        await web.UnixSite(runner, str(endpoint)).start()
        os.chmod(endpoint, 0o600)
        monitor = asyncio.create_task(worker.monitor())
        monitor.add_done_callback(lambda _task: stop.set())
        print(f"Runtime {service['kind']} service ready: {capability}, run {run}", flush=True)
        await stop.wait()
    finally:
        try:
            if monitor:
                monitor.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor
        finally:
            try:
                await worker.close()
            finally:
                if runner:
                    await runner.cleanup()
                endpoint.unlink(missing_ok=True)
                glib.quit()
                native_thread.join(timeout=3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--capability", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.profile, args.state, args.capability))
    except (Fault, OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
