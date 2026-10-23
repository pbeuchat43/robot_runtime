"""Real Foxglove WebSocket checks against isolated, harmless ROS fixtures."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid

import aiohttp
from robot_runtime.ipc import request
from foxglove_fixture import make_profile
from integration_browser import wait_for


async def main(root):
    root = root.resolve()
    assert root.parent == Path("/tmp") and re.fullmatch(r"robot-runtime-ros-[0-9a-f]{12}", root.name)
    os.umask(0o077)
    token = uuid.uuid4().hex[:10]
    area = root / ("foxglove-" + token)
    area.mkdir()
    profile = make_profile(area, token)
    state, namespace = area / "state", "/dr_fox_" + token
    bridge_port = int((area / "bridge-port.txt").read_text())
    report = dict(passed=False, checks=[], cleanup_errors=[], domain_id=230, address="127.0.0.1", port=bridge_port)
    log = (area / "test.log").open("w")
    manager, provider = None, None
    collision = socket.socket()
    collision.bind(("127.0.0.1", bridge_port))
    collision.listen()
    async def local(action, **values): return await asyncio.to_thread(request, state, action, **values)
    async def status(): return await local("status", fresh=True)
    async def unit(): return (await status())["units"][0]
    async def lifecycle(action, **values):
        return await local(action, unit_id="idle", request_id=str(uuid.uuid4()), expires_at_ms=int(time.time()*1000)+10000,
                           config_revision=(await status())["config_revision"], **values)
    async def ready(): return (await status())["fresh"] and (await local("ros_status"))["available"]
    async def stopped(): return not (await unit())["blocked"]
    async def observed(key, expected):
        try: return json.loads((area / "provider-status.json").read_text()).get(key) == expected
        except (FileNotFoundError, json.JSONDecodeError): return False
    def check(name, **values): report["checks"].append(dict(check=name, passed=True, **values))
    def launch_manager():
        return subprocess.Popen([sys.executable, "-m", "robot_runtime.manager", "--profile", str(area / "profile.json"), "--state", str(state)], stdout=log, stderr=log)
    try:
        manager = launch_manager()
        await wait_for(ready, seconds=20)
        started = await lifecycle("start")
        async def failed():
            value = await unit()
            return value if value["run_id"] == started["run_id"] and value["lifecycle"] == "failed" and not value["blocked"] else None
        failure = await wait_for(failed)
        assert collision.getsockname()[1] == bridge_port and collision.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
        check("occupied_port_fails_without_replacing_existing_listener", unit=failure)
        collision.close()
        provider = subprocess.Popen(["/bin/bash", "--noprofile", "--norc", str(area / "launch.sh"), "/usr/bin/python3",
            str(Path(__file__).with_name("foxglove_fixture.py")), str(area), namespace], env=profile["units"][0]["environment"], stdout=log, stderr=log)
        started = await lifecycle("start")
        async with aiohttp.ClientSession() as client:
            async def connect():
                try:
                    return await client.ws_connect(f"http://127.0.0.1:{bridge_port}", protocols=["foxglove.sdk.v1"])
                except aiohttp.WSServerHandshakeError as exc:
                    raise AssertionError("Foxglove handshake failed: " + repr(exc)) from exc
            ws = await wait_for(connect)
            async with ws:
                channels, services = {}, {}
                async def receive(predicate, seconds=15):
                    deadline = time.monotonic() + seconds
                    while time.monotonic() < deadline:
                        message = await ws.receive(timeout=max(.1, deadline-time.monotonic()))
                        if message.type == aiohttp.WSMsgType.TEXT:
                            value = json.loads(message.data)
                            if value.get("op") == "advertise": channels.update({item["topic"]: item for item in value["channels"]})
                            if value.get("op") == "advertiseServices": services.update({item["name"]: item for item in value["services"]})
                        elif message.type == aiohttp.WSMsgType.BINARY: value = message.data
                        else: raise AssertionError("Unexpected Foxglove connection closure: " + str(message))
                        result = predicate(value)
                        if result: return result
                    raise AssertionError("Foxglove response timed out")
                info = await receive(lambda value: value if isinstance(value, dict) and value.get("op") == "serverInfo" else None)
                assert {"clientPublish", "parameters", "services", "connectionGraph", "assets"} <= set(info["capabilities"])
                check("existing_capabilities_advertised_without_runtime_login", server_info=info)
                await receive(lambda _: channels.get(namespace + "/output") and services.get(namespace + "/enable"))
                channel = channels[namespace + "/output"]
                assert channel["schemaName"] == "std_msgs/msg/String" and channel["encoding"] == "cdr"
                await ws.send_json(dict(op="subscribe", subscriptions=[dict(id=1, channelId=channel["id"])]))
                sample = await receive(lambda value: value if isinstance(value, bytes) and value[0] == 1 and struct.unpack_from("<I", value, 1)[0] == 1 else None)
                assert b"foxglove fixture" in sample[13:]
                await ws.send_json(dict(op="unsubscribe", subscriptionIds=[1]))
                check("native_ros_topic_and_schema_reach_websocket", topic=channel["topic"], schema=channel["schemaName"], message_bytes=len(sample))
                await ws.send_json(dict(op="advertise", channels=[dict(id=7, topic=namespace + "/input", encoding="cdr", schemaName="std_msgs/msg/String")]))
                await wait_for(lambda: observed("input_publishers", 1))
                text = b"harmless client message\0"
                await ws.send_bytes(struct.pack("<BI", 1, 7) + b"\0\1\0\0" + struct.pack("<I", len(text)) + text)
                await wait_for(lambda: observed("received", ["harmless client message"]))
                await ws.send_json(dict(op="unadvertise", channelIds=[7]))
                check("client_publish_reaches_only_the_harmless_fixture")
                service_id = services[namespace + "/enable"]["id"]
                await ws.send_bytes(struct.pack("<BIII", 2, service_id, 9, 3) + b"cdr" + b"\0\1\0\0\1")
                response = await receive(lambda value: value if isinstance(value, bytes) and value[0] == 3 and struct.unpack_from("<I", value, 5)[0] == 9 else None)
                encoding_length = struct.unpack_from("<I", response, 9)[0]
                assert response[13+encoding_length+4] == 1
                await wait_for(lambda: observed("calls", [True]))
                check("service_call_returns_native_fixture_response")
                await ws.send_json(dict(op="getParameters", parameterNames=[], id="get-fixture"))
                values = await receive(lambda value: value if isinstance(value, dict) and value.get("op") == "parameterValues" and value.get("id") == "get-fixture" else None)
                gain, = [item for item in values["parameters"] if item["name"].startswith(namespace + "/fixture") and item["name"].endswith("gain")]
                assert gain["value"] == 1.
                await ws.send_json(dict(op="setParameters", parameters=[dict(name=gain["name"], value=2.5, type="float64")], id="set-fixture"))
                await receive(lambda value: isinstance(value, dict) and value.get("op") == "parameterValues" and value.get("id") == "set-fixture")
                await wait_for(lambda: observed("gain", 2.5))
                check("parameter_read_and_write_preserved", parameter=gain["name"])
                async def settings_available():
                    return (await local("ros_status"))["parameters"][0]["available"]
                await wait_for(settings_available)
                await local("ros_parameters", capability_id="foxglove-settings", refresh=True)
                async def settings():
                    value = await local("ros_parameters", capability_id="foxglove-settings", refresh=False)
                    report["settings_last"] = value
                    return value if not value["reading"] else None
                values = await wait_for(settings)
                assert values["fresh"], values
                assert len(values["parameters"]) == 15 and all(not item["writable"] for item in values["parameters"].values())
                assert values["parameters"]["port"]["value"] == str(bridge_port)
                check("runtime_settings_are_read_only", settings=values)
                report.pop("settings_last", None)
                current = await unit()
                manager.terminate()
                await asyncio.to_thread(manager.wait, timeout=10)
                manager = launch_manager()
                await wait_for(ready)
                assert (await unit())["run_id"] == started["run_id"] and (await unit())["main_pid"] == current["main_pid"]
                await ws.send_json(dict(op="getParameters", parameterNames=[gain["name"]], id="after-recovery"))
                await receive(lambda value: isinstance(value, dict) and value.get("id") == "after-recovery")
                check("management_restart_preserves_bridge_run_and_connection")
                await lifecycle("stop", expected_run=started["run_id"])
                await wait_for(stopped)
                # Drain any final graph update until the actual close is observed.
                for _ in range(20):
                    message = await ws.receive(timeout=5)
                    if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED): break
                else: raise AssertionError("Bridge did not close its client")
            with socket.socket() as probe: assert probe.connect_ex(("127.0.0.1", bridge_port)) != 0
            check("graceful_stop_closes_connection_and_releases_port")
            restarted = await lifecycle("start")
            async with await wait_for(connect) as reconnected:
                info = await reconnected.receive_json(timeout=10)
                assert info["op"] == "serverInfo" and restarted["run_id"] != started["run_id"]
            await lifecycle("stop", expected_run=restarted["run_id"])
            await wait_for(stopped)
            check("explicit_new_start_accepts_a_fresh_connection")
        report["passed"] = True
    except BaseException as exc:
        report["error"] = repr(exc)
        raise
    finally:
        collision.close()
        if manager and manager.poll() is None:
            try:
                current = await unit()
                if current["blocked"] and not current.get("stop_request_id"): await lifecycle("stop", expected_run=current["run_id"])
                await wait_for(stopped)
                report["final_status"] = await status()
            except Exception as exc: report["cleanup_errors"].append(repr(exc))
            manager.terminate()
            await asyncio.to_thread(manager.wait, timeout=10)
        if provider and provider.poll() is None:
            provider.send_signal(signal.SIGINT)
            await asyncio.to_thread(provider.wait, timeout=10)
        log.close()
        (area / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        (root / "latest-foxglove.txt").write_text(area.name)
        print(json.dumps(dict(passed=report["passed"], checks=len(report["checks"]), cleanup_errors=report["cleanup_errors"], path=str(area))))
        assert not report["cleanup_errors"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    asyncio.run(main(parser.parse_args().root))
