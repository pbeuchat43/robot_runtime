"""Optional HTTPS browser adapter; no direct supervisor authority."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import argparse
import asyncio
import contextlib
import hmac
import ssl
import time
import uuid
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from .auth import Auth, COOKIE
from .av_web import AVGateway
from .common import Fault, dumps, fields, loads
from .ipc import MAX_RESPONSE, decode_response, request as local_request
from .ros_web import TopicFanout
from .resource_web import ResourceFanout
from .ros_profile import MAX_TOPICS
from .stream_web import stream_events

STATUS_CODES = {"unauthorized": 401, "forbidden": 403, "not_found": 404, "rate_limited": 429,
                "session_limit": 429, "busy": 409, "stale_run": 409, "stale_config": 409,
                "request_conflict": 409, "ownership_conflict": 409, "unit_conflict": 409, "stop_already_requested": 409,
                "transport_unknown": 503, "supervisor_unavailable": 503, "outcome_unknown": 409,
                "ros_unavailable": 503, "av_unavailable": 503, "stale_generation": 409, "stale_snapshot": 409, "stale_goal": 409, "read_only": 403}


class BrowserAdapter:
    def __init__(self, state, origin, credential):
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise Fault("invalid_origin", "Configure an exact HTTPS origin without a path")
        self.state, self.origin, self.host = state, origin, parsed.netloc
        self.auth = Auth(credential)
        self.verifying = False
        self.rpc_slots = asyncio.Semaphore(8)
        self.snapshot_slots = asyncio.Semaphore(5)
        self.viewers = set()
        self.stream_viewers = set()
        self.closing = False
        self.latest = dict(api_version=1, units=[], fresh=False, error="Waiting for manager", age_seconds=None)
        self.received = time.monotonic()
        self.generation = uuid.uuid4().hex
        self.ros = TopicFanout(state)
        self.resources = ResourceFanout(state)
        self.av = AVGateway(self)

    async def rpc(self, action, **values):
        if self.rpc_slots.locked():
            raise Fault("busy", "Web request limit reached")
        async with self.rpc_slots:
            return await asyncio.to_thread(local_request, self.state, action, **values)

    def snapshot(self):
        result = dict(self.latest, web_generation=self.generation, server_time_ms=int(time.time() * 1000))
        elapsed = time.monotonic() - self.received
        if result.get("age_seconds") is not None:
            result["age_seconds"] += elapsed
        result["fresh"] = bool(result.get("fresh") and result.get("age_seconds", 99) <= 15)
        result["units"] = [dict(unit, fresh=result["fresh"]) for unit in result.get("units", [])]
        return result

    def publish(self, value):
        self.latest = value
        self.received = time.monotonic()
        for channel in tuple(self.viewers):
            if channel.full():
                channel.get_nowait()
            channel.put_nowait(True)

    async def upstream(self):
        while True:
            writer = None
            try:
                reader, writer = await asyncio.open_unix_connection(str(Path(self.state) / "runtime.sock"), limit=MAX_RESPONSE + 1)
                writer.write(b'{"version":1,"action":"watch"}\n')
                await writer.drain()
                while True:
                    value = decode_response(await asyncio.wait_for(reader.readline(), 15))
                    if value.get("type") == "snapshot":
                        self.publish(value["status"])
            except (OSError, Fault, ValueError, asyncio.TimeoutError):
                self.publish(dict(self.snapshot(), fresh=False, error="Manager connection unavailable"))
            finally:
                if writer:
                    writer.close()
                    with contextlib.suppress(OSError):
                        await writer.wait_closed()
            await asyncio.sleep(1)

    def session(self, request, *, required=False, csrf=False):
        session = self.auth.session(request.cookies.get(COOKIE), touch=csrf)
        if required and not session:
            raise Fault("unauthorized", "Unlock commands with the robot passphrase")
        if csrf and (not session or not hmac.compare_digest(request.headers.get("X-CSRF-Token", "").encode(), session["csrf"].encode())):
            raise Fault("forbidden", "Session CSRF token is missing or incorrect")
        return session

    async def body(self, request):
        if request.content_type != "application/json":
            raise Fault("invalid_request", "Expected application/json")
        return loads(await request.read())

    async def login(self, request):
        value = await self.body(request)
        fields(value, ("passphrase",))
        self.auth.allow_login()
        if self.verifying:
            raise Fault("busy", "Another credential verification is in progress")
        self.verifying = True
        signature = self.auth.signature
        try:
            valid = await asyncio.to_thread(self.auth.verify, value["passphrase"])
        finally:
            self.verifying = False
        self.auth.reload()
        if not valid or self.auth.signature != signature:
            raise Fault("unauthorized", "Incorrect robot passphrase")
        # A new login replaces this browser's previous session.
        self.auth.revoke(request.cookies.get(COOKIE))
        token, session = self.auth.issue()
        response = web.json_response(dict(authenticated=True, csrf=session["csrf"], session_id=session["id"]))
        response.set_cookie(COOKIE, token, secure=True, httponly=True, samesite="Strict", path="/", max_age=8 * 3600)
        return response

    async def logout(self, request):
        self.session(request, required=True, csrf=True)
        fields(await self.body(request), ())
        self.auth.revoke(request.cookies.get(COOKIE))
        response = web.json_response(dict(authenticated=False))
        response.del_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="Strict")
        return response

    async def session_info(self, request):
        session = self.session(request)
        return web.json_response(dict(authenticated=bool(session), csrf=session["csrf"] if session else None,
                                      session_id=session["id"] if session else None))

    async def status(self, _request):
        return web.json_response(self.snapshot())

    async def command(self, request):
        session = self.session(request, required=True, csrf=True)
        action = request.match_info["action"]
        if action not in ("start", "stop", "prepare-launch"):
            raise Fault("not_found", "Unknown command")
        value = await self.body(request)
        if action == "prepare-launch":
            fields(value, ("config_revision",), ("mode",))
            return web.json_response(await self.rpc("prepare_launch", unit_id=request.match_info["unit"], **value))
        fields(value, ("request_id", "expires_at_ms", "config_revision"), ("expected_run", "mode"))
        result = await self.rpc(action, unit_id=request.match_info["unit"], actor="browser:" + session["id"], **value)
        return web.json_response(result, status=202)

    async def operation(self, request):
        operation = await self.rpc("operation", request_id=request.match_info["request_id"])
        if operation.get("action") in ("ros_service", "ros_parameter_set", "ros_action_goal", "ros_action_cancel", "ros_topic_command"):
            self.session(request, required=True)
        return web.json_response(operation)

    async def ros_status(self, _request):
        return web.json_response(await self.rpc("ros_status"))

    async def stream_events(self, request):
        return await stream_events(self, request)

    async def ros_actions(self, request):
        if request.query:
            raise Fault("invalid_request", "Action observations take no query fields")
        return web.json_response(await self.rpc("ros_actions"))

    async def ros_topic_command(self, request):
        session = self.session(request, required=True, csrf=True)
        value = await self.body(request)
        fields(value, ("request_id", "expires_at_ms", "config_revision", "expected_run", "generation", "choice"))
        result = await self.rpc("ros_topic_command", capability_id=request.match_info["capability"], actor="browser:" + session["id"], **value)
        return web.json_response(result, status=202)

    async def ros_action_command(self, request):
        session = self.session(request, required=True, csrf=True)
        action = "ros_action_goal" if request.match_info["command"] == "goals" else "ros_action_cancel"
        value = await self.body(request)
        fields(value, ("request_id", "expires_at_ms", "config_revision", "expected_run", "generation",
                       "arguments" if action == "ros_action_goal" else "goal_request_id"))
        result = await self.rpc(action, capability_id=request.match_info["capability"], actor="browser:" + session["id"], **value)
        return web.json_response(result, status=202)

    async def image_snapshot(self, request):
        started = time.monotonic()
        value = await self.body(request)
        fields(value, ("expected_run", "generation", "config_revision"))
        if self.closing or self.snapshot_slots.locked():
            raise Fault("busy", "Snapshot viewer limit reached")
        async with self.snapshot_slots:
            writer = task = None
            try:
                reader, writer = await asyncio.open_unix_connection(str(Path(self.state) / "runtime.sock"), limit=MAX_RESPONSE + 1)
                writer.write((dumps(dict(version=1, action="ros_snapshot", capability_id=request.match_info["capability"], **value)) + "\n").encode())
                await writer.drain()
                task = asyncio.create_task(reader.readline())
                while not task.done():
                    if (self.closing or request.transport is None or request.transport.is_closing()
                            or time.monotonic() - started > 12):
                        raise Fault("snapshot_unavailable", "Snapshot viewer disconnected or deadline exceeded")
                    await asyncio.wait([task], timeout=0.2)
                result = decode_response(task.result())
                receipt_age = max(0, time.monotonic() - result["received_monotonic"])
                result.update(receipt_age_seconds=receipt_age,
                              acquisition_age_seconds=result["acquisition_age_at_receipt"] + receipt_age,
                              request_duration_seconds=time.monotonic() - started)
                if result["acquisition_age_seconds"] > result["stale_after_seconds"]:
                    raise Fault("stale_snapshot", "Snapshot was stale before browser delivery")
                return web.json_response(result)
            except (OSError, ValueError) as exc:
                raise Fault("snapshot_unavailable", "Snapshot manager connection unavailable") from exc
            finally:
                if task:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, OSError, ValueError):
                        await task
                if writer:
                    writer.close()
                    with contextlib.suppress(OSError):
                        await writer.wait_closed()

    async def ros_service(self, request):
        session = self.session(request, required=True, csrf=True)
        value = await self.body(request)
        fields(value, ("request_id", "expires_at_ms", "config_revision", "expected_run", "generation", "arguments"))
        result = await self.rpc("ros_service", capability_id=request.match_info["capability"], actor="browser:" + session["id"], **value)
        return web.json_response(result, status=202)

    async def ros_parameters(self, request):
        if set(request.query) - {"refresh"} or len(request.query.getall("refresh", [])) > 1 or request.query.get("refresh", "0") not in ("0", "1"):
            raise Fault("invalid_request", "Use refresh=1 to inspect current parameter values")
        return web.json_response(await self.rpc("ros_parameters", capability_id=request.match_info["capability"],
                                               refresh=request.query.get("refresh") == "1"))

    async def ros_parameter_set(self, request):
        session = self.session(request, required=True, csrf=True)
        value = await self.body(request)
        fields(value, ("request_id", "expires_at_ms", "config_revision", "expected_run", "generation", "expected_snapshot", "changes"))
        result = await self.rpc("ros_parameter_set", capability_id=request.match_info["capability"], actor="browser:" + session["id"], **value)
        return web.json_response(result, status=202)

    async def ros_events(self, request):
        if set(request.query) != {"topic"}:
            raise Fault("invalid_request", "Choose ROS topics with repeated topic query fields")
        selected = request.query.getall("topic")
        catalog = await self.rpc("ros_status")
        known = {topic["id"] for topic in catalog["topics"]}
        if not 1 <= len(selected) <= MAX_TOPICS or not set(selected) <= known:
            raise Fault("not_found", "Unknown ROS topic capability")
        interval = 1 / max(topic["max_rate_hz"] for topic in catalog["topics"] if topic["id"] in selected)
        channel = await self.ros.add(selected)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        try:
            await response.prepare(request)
            next_write = time.monotonic()
            while not self.ros.closing:
                try:
                    await asyncio.wait_for(channel.get(), 5)
                    # A wakeup represents new state, not a frame to queue. Coalesce
                    # independently arriving topics before taking the latest view.
                    await asyncio.sleep(max(0, next_write - time.monotonic()))
                    if self.ros.closing:
                        break
                    while not channel.empty():
                        channel.get_nowait()
                    data = ("data: " + dumps(self.ros.snapshot(selected)) + "\n\n").encode()
                except asyncio.TimeoutError:
                    data = b": heartbeat\n\n"
                next_write = time.monotonic() + interval
                await asyncio.wait_for(response.write(data), 2)
        except (ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            await self.ros.remove(channel)
        return response

    async def resource_status(self, request):
        if set(request.query) - {"history"} or request.query.get("history", "false") not in ("true", "false"):
            raise Fault("invalid_request", "Use history=true or history=false")
        return web.json_response(await self.rpc("resource_status", history=request.query.get("history") == "true"))

    async def resource_events(self, request):
        if request.query:
            raise Fault("invalid_request", "Resource events take no query fields")
        channel = await self.resources.add()
        revision = None
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        try:
            await response.prepare(request)
            while not self.resources.closing:
                try:
                    await asyncio.wait_for(channel.get(), 5)
                    if self.resources.closing:
                        break
                    value = self.resources.snapshot(history=revision != self.resources.stream_revision)
                    revision = self.resources.stream_revision
                    data = ("data: " + dumps(value) + "\n\n").encode()
                except asyncio.TimeoutError:
                    data = b": heartbeat\n\n"
                await asyncio.wait_for(response.write(data), 2)
        except (ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            await self.resources.remove(channel)
        return response

    async def logs(self, request):
        run_id = request.match_info["run"]
        target = await self.rpc("log_target", run_id=run_id)
        if not target["public_logs"]:
            self.session(request, required=True)
        if set(request.query) - {"cursor", "limit"}:
            raise Fault("invalid_request", "Unsupported log query")
        try:
            limit = int(request.query.get("limit", "100"))
        except ValueError as exc:
            raise Fault("invalid_request", "Invalid log limit") from exc
        return web.json_response(await self.rpc("logs", run_id=run_id, cursor=request.query.get("cursor"), limit=limit))

    async def events(self, request):
        if self.closing or len(self.viewers) >= 8:
            raise Fault("busy", "Observation connection limit reached")
        channel = asyncio.Queue(maxsize=1)
        self.viewers.add(channel)
        channel.put_nowait(True)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store",
                                               "X-Accel-Buffering": "no"})
        try:
            await response.prepare(request)
            while not self.closing:
                try:
                    await asyncio.wait_for(channel.get(), 5)
                    if self.closing:
                        break
                    value = self.snapshot()
                    data = ("data: " + dumps(value) + "\n\n").encode()
                except asyncio.TimeoutError:
                    data = b": heartbeat\n\n"
                await asyncio.wait_for(response.write(data), 2)
        except (ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            self.viewers.discard(channel)
        return response


def application(state, origin, credential):
    adapter = BrowserAdapter(state, origin, credential)

    @web.middleware
    async def guard(request, handler):
        try:
            if request.host != adapter.host:
                raise Fault("forbidden", "Host does not match the configured robot origin")
            if request.method not in ("GET", "HEAD") and request.headers.get("Origin") != adapter.origin:
                raise Fault("forbidden", "Origin does not match the configured robot origin")
            adapter.auth.reload()
            response = await handler(request)
        except Fault as exc:
            response = web.json_response(dict(error=exc.as_dict()), status=STATUS_CODES.get(exc.code, 400))
        except web.HTTPException as exc:
            response = web.json_response(dict(error=dict(code="http_error", message=exc.reason)), status=exc.status)
        if not response.prepared:
            response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                     "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                     "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' blob:; media-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        return response

    app = web.Application(middlewares=[guard], client_max_size=16384)
    app["adapter"] = adapter
    async def shutdown(_app):
        # Wake long-lived readers before aiohttp waits for request handlers.
        adapter.closing = True
        for channel in adapter.viewers | adapter.stream_viewers:
            if channel.full():
                channel.get_nowait()
            channel.put_nowait(False)
        await adapter.ros.close()
        await adapter.resources.close()
        await adapter.av.close()
    app.on_shutdown.append(shutdown)
    async def lifecycle(_app):
        task = asyncio.create_task(adapter.upstream())
        av_task = asyncio.create_task(adapter.av.heartbeat())
        yield
        task.cancel()
        av_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with contextlib.suppress(asyncio.CancelledError):
            await av_task
    app.cleanup_ctx.append(lifecycle)
    async def asset(request):
        name = request.match_info.get("name") or {"/autonomous": "autonomous.html", "/outreach": "outreach.html",
            "/video": "av.html", "/input": "av.html", "/camera-gamepad": "av.html"}.get(request.path, "index.html")
        types = {"index.html": "text/html", "app.js": "application/javascript", "ros.js": "application/javascript",
                 "parameters.js": "application/javascript", "observations.js": "application/javascript",
                 "snapshots.js": "application/javascript", "imu.js": "application/javascript", "spatial.js": "application/javascript",
                 "spatial3d.js": "application/javascript", "resources.js": "application/javascript", "actions.js": "application/javascript", "style.css": "text/css",
                 **{f"client-{name}.mjs": "application/javascript" for name in ("core", "http", "sse", "stream", "multiplex", "page")},
                 "views-runtime.mjs": "application/javascript", "page-monitor.mjs": "application/javascript", "launch-control.mjs": "application/javascript",
                 "autonomous.html": "text/html", "outreach.html": "text/html", "av.html": "text/html"}
        av_assets = {"core.js", "runtime-api.js", "page.js", "page.css", "gamepad-art.js", "gamepad-input.js", "control-client.js",
            "media-client.js", "components.css", "freshness.js", "vendor/gstwebrtc-api-3.0.0.esm.js",
            *{"components/" + part + ".js" for part in ("camera-picker", "control-panel", "diagnostics", "gamepad-view", "stream-settings", "video-view", "source-controls")}}
        types.update({"av/" + part: "text/css" if part.endswith(".css") else "application/javascript" for part in av_assets})
        if name not in types:
            raise web.HTTPNotFound()
        return web.Response(body=files("robot_runtime").joinpath("static", name).read_bytes(), content_type=types[name])
    app.router.add_get("/", asset)
    app.router.add_get("/autonomous", asset)
    app.router.add_get("/outreach", asset)
    for page in ("/video", "/input", "/camera-gamepad"):
        app.router.add_get(page, asset)
    app.router.add_get("/assets/{name:.*}", asset)
    app.router.add_get("/api/status", adapter.status)
    app.router.add_get("/api/av", adapter.av.catalog)
    app.router.add_get("/api/av/{capability}/signal", adapter.av.signal)
    app.router.add_get("/api/av/{capability}/{route:.*}", adapter.av.handle)
    app.router.add_post("/api/av/{capability}/{route:.*}", adapter.av.handle)
    app.router.add_get("/api/session", adapter.session_info)
    app.router.add_post("/api/login", adapter.login)
    app.router.add_post("/api/logout", adapter.logout)
    app.router.add_get("/api/events", adapter.events)
    app.router.add_get("/api/stream", adapter.stream_events)
    app.router.add_get("/api/resources", adapter.resource_status)
    app.router.add_get("/api/resources/events", adapter.resource_events)
    app.router.add_post("/api/units/{unit}/{action}", adapter.command)
    app.router.add_get("/api/operations/{request_id}", adapter.operation)
    app.router.add_get("/api/runs/{run}/logs", adapter.logs)
    app.router.add_get("/api/ros", adapter.ros_status)
    app.router.add_get("/api/ros/actions", adapter.ros_actions)
    app.router.add_post("/api/ros/commands/{capability}", adapter.ros_topic_command)
    app.router.add_post("/api/ros/actions/{capability}/{command:goals|cancel}", adapter.ros_action_command)
    app.router.add_post("/api/ros/snapshots/{capability}", adapter.image_snapshot)
    app.router.add_get("/api/ros/events", adapter.ros_events)
    app.router.add_post("/api/ros/services/{capability}", adapter.ros_service)
    app.router.add_get("/api/ros/parameters/{capability}", adapter.ros_parameters)
    app.router.add_post("/api/ros/parameters/{capability}", adapter.ros_parameter_set)
    return app


def main():
    parser = argparse.ArgumentParser(description="Serve the runtime browser interface over HTTPS")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--credential", required=True, type=Path)
    parser.add_argument("--cert", required=True, type=Path)
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8443)
    args = parser.parse_args()
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.cert, args.key)
        app = application(args.state, args.origin, args.credential)
        web.run_app(app, host=args.bind, port=args.port, ssl_context=context, access_log=None, shutdown_timeout=5)
    except (Fault, OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
